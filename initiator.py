# Initiator class will initiate contact with participant to start the atomic swap

from __future__ import print_function

import grpc
import time
import datetime

import atomicswap_pb2
import atomicswap_pb2_grpc
import itertools
from optparse import OptionParser

import sys
import subprocess
import os
import json
from collections import namedtuple

from dry_run import InitiatorDryRun

def _json_object_hook(d): return namedtuple('X', d.keys())(*d.values())
def json2obj(data): return json.loads(data, object_hook=_json_object_hook)

def print_rt(output):
    output = "{}\n".format(str(output))
    sys.stdout.write(output)
    sys.stdout.flush()

def print_json(step, stepName, data):
    jsonObject = {}
    jsonObject['step'] = step
    jsonObject['stepName'] = stepName
    jsonObject['data'] = data
    json_data = json.dumps(jsonObject) 
    json_data = "{}\n".format(str(json_data)) 
    sys.stdout.write(json_data)
    sys.stdout.flush()

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

class AtomicSwap():

    def verboseLog(self, m):
        if self.verbose:
            eprint('\n['+datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")+']: \n'+m+'\n')

    def __init__(self, init_amount, part_amount, host, dry_run, verbose):
        
        self.init_amount = float(init_amount)
        self.part_amount = float(part_amount)
        self.dry_run = dry_run
        self.host = host
        self.verbose = verbose

    def execute(self, process):
 
        if self.dry_run:
            dry = InitiatorDryRun(self.init_amount, self.part_amount)
            return dry.processCommand(process)
        
        pro = subprocess.Popen(process, stdout=subprocess.PIPE, 
                            shell=True, preexec_fn=os.setsid) 
        out, err = pro.communicate()
        if self.verbose:
            print('Out:\n' + str(out))
            print('\nErr:\n' + str(err))
            print('\nCode:\n' + str(pro.returncode))
        return out, err, pro.returncode
        
    def run(self):

        #######GLOSSARY######
        # init = initiator
        # part = participant
        # ctc = contract
        # tx = transaction
        # addr = address
        #####################

        #### STEP 1 ####
        self.verboseLog('Step 1: Initiating exchange, the amounts to exchange are:\nInitiator: ' + str(self.init_amount) + '\nParticipant: ' + str(self.part_amount))

        channel = grpc.insecure_channel(self.host + ':50051')
        stub = atomicswap_pb2_grpc.AtomicSwapStub(channel)
        self.verboseLog('Opened grpc connection.')

        self.init_addr, _, _ = self.execute("tfchainc wallet address")
        self.init_addr = self.init_addr[21:] # removing substring, JSON output in future?
        self.init_addr = self.init_addr.rstrip("\r\n")
        self.verboseLog('Generated TFT Address for Participant to build contract with: ' + self.init_addr)

        self.verboseLog('Sending TFT Address to Participant and asking for amount confirmation...')

        #order of startup does not matter anymore if we try to connect a few times

        for x in range(0, 10):
            # Print for UI
            try:
                response = stub.ProcessInitiate(atomicswap_pb2.Initiate(init_amount=self.init_amount, part_amount=self.part_amount, init_addr=self.init_addr))
                break
            except Exception:
                time.sleep(5)
                print("Sleeping for connect")
                if x == 10:
                    exit(1)

        self.verboseLog('Participant confirmed amounts and sent BTC Address: ' + response.part_addr)

            # JSON for UI
        print_json(1, "Received confirmation from Participant, Exchanged recipient addresses", self.step_one_data(response))

        #### STEP 2 ####
        self.verboseLog('Step 2: Creating BTC atomicswap contract with the Participant Address')

        init_ctc_cmd = "btcatomicswap --testnet --rpcuser=user --rpcpass=pass -s localhost:8332 initiate {} {}".format(response.part_addr, self.init_amount)
        init_ctc_json, init_ctc_err, init_ctc_code = self.execute(init_ctc_cmd)
        init_ctc = json2obj(init_ctc_json)
        self.init_ctc_hex = init_ctc.contractHex
        self.init_ctc_tx_hex = init_ctc.transactionHex
        self.verboseLog('Created BTC atomicswap contract:\n' + init_ctc_json)

        self.verboseLog('Sending BTC atomicswap contract details to Participant...')
        response = stub.ProcessInitiateSwap(atomicswap_pb2.InitiateSwap(init_ctc_redeem_addr=init_ctc.redeemAddr, init_ctc_hex=init_ctc.contractHex, init_ctc_tx_hex=init_ctc.transactionHex, hashed_secret=init_ctc.hashedSecret))
        if response.accepted is False:
            self.verboseLog('Auditing Failed on Participant Side, aborting swap')
            self.refund(173500) #48hrs wait

        self.verboseLog('Participant created TFT atomicswap contract. The Redeem address is: ' + response.part_ctc_redeem_addr)

            # Print for UI
        print_json(2, "Atomicswap Contracts created, waiting until visible", self.step_two_data(init_ctc, response))

        self.verboseLog('Waiting until TFT contract is visible on explorer, this may take a while...')
        self.waitUntilTxVisible(response.part_ctc_redeem_addr)
        self.verboseLog('A block with the Redeem address was found.')

        self.verboseLog('Auditing TFT Contract')
        _, _, auditCode = self.execute('tfchainc atomicswap --encoding json -y auditcontract {} --amount {} --receiver {} --secrethash {}'.format(response.part_ctc_redeem_addr, self.part_amount, self.init_addr, init_ctc.hashedSecret))
        auditCode = 1
        if auditCode is 0:
            self.verboseLog('Audit Successful')
        else:
            self.verboseLog('Audit Failed, aborting swap')
            stub.ProcessAbort(atomicswap_pb2.Abort(abort=True))
            self.refund(87000) # wait 24hrs, because participant waited 24hrs also


        #### Step 3 ####
        self.verboseLog('Proceeding to Step 3: Redeeming Participant contract and revealing secret')

            # Make Redeem Transaction
        part_ctc_redeem_json,_,redeemCode = self.execute("tfchainc atomicswap --encoding json -y redeem {} {}".format(response.part_ctc_redeem_addr, init_ctc.secret))
        if redeemCode is 0:
            self.verboseLog('Redeem Successful')
        else:
            self.verboseLog('Redeem Failed, aborting swap')
            stub.ProcessAbort(atomicswap_pb2.Abort(abort=True))
            self.refund(87000) # wait 24hrs, because participant waited 24hrs also
        self.verboseLog('Redeemed TFT contract: ' + part_ctc_redeem_json)

            # RPC #3 to Participant,
            # IF Participant makes Redeem Transaction,
            # Returns a Finished = True message
        self.verboseLog('Sending secret to Participant and Awaiting response...')
        response = stub.ProcessRedeemed(atomicswap_pb2.InitiatorRedeemFinished(secret=init_ctc.secret))
        self.verboseLog('Participant redeemed BTC contract')

            # Print Step Info
        print_json(3, "Redeem Transactions created, Atomicswap Finished", self.step_three_data(response))


        self.verboseLog('Atomicswap completed.')


    #########################
    # r = response
    # ic = init_ctc
    #########################

    def step_one_data(self, r):
        data = {}
        data['participantAmount'] = self.part_amount
        data['initiatorAmount'] = self.init_amount
        data['participantAddress'] = r.part_addr
        data['initiatorAddress'] = self.init_addr
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def step_two_data(self, ic, r):
        data = {}
        data['initiatorContractRedeemAddress'] = ic.redeemAddr
        data['initiatorContractContractHex'] = ic.contractHex
        data['initiatorContractTransactionHex'] = ic.transactionHex
        data['participantContractRedeemAddress'] = r.part_ctc_redeem_addr
        data['initiatorAddress'] = self.init_addr
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def step_three_data(self, r):
        data = {}
        data['atomicswapFinished'] = r.finished
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def waitUntilTxVisible(self, hash):
        # Should probably have a max number of tries

        returncode = 1
        counter = 0
        spinner = itertools.cycle(['-', '/', '|', '\\'])

        while returncode != 0:
            sys.stderr.write(spinner.next())
            sys.stderr.flush()
            sys.stderr.write('\b')
            counter += 1
            time.sleep(0.1)
            if (counter % 100) == 0:
                self.verboseLog('Looking up hash in explorer:')
                _, _, returncode = self.execute("tfchainc explore hash "+ hash)
                self.verboseLog('Waiting 10s')

    def refund(self, seconds):
        self.verboseLog('Going to wait ' + str(seconds) + 'seconds... until we can refund...')
        time.sleep(seconds)
        init_refund_json,init_refund_err,refundCode = self.execute('btcatomicswap --testnet --rpcuser=user --rpcpass=pass -s localhost:8332 refund {} {}'.format(self.init_ctc_hex, self.init_ctc_tx_hex))
        #init_refund = json2obj(init_refund_json)
        if refundCode is 0:
            self.verboseLog('Executed refund transaction: ' + init_refund_json)
        else:
            self.verboseLog('Something went wrong with publishing the refund: ' + init_refund_json + '\n' + init_refund_err)
        exit(1)




if __name__ == '__main__':
    
    parser = OptionParser()

    parser.add_option("-m", "--my-amount", dest="init_amount",
                    help="Your amount of your currency to swap", metavar="INITIATORAMOUNT")

    parser.add_option("-o", "--other-amount",
                    dest="part_amount", default=True,
                    help="The amount of the other partners currency to swap")

    parser.add_option("-i", "--ipaddr",
                    dest="host", default=True,
                    help="The host")

    parser.add_option("-d", "--dry-run", action="store_true",
                        dest="dry_run",  help="Do a dry run with dummy data")

    parser.add_option("-v", "--verbose", action="store_true",
                        dest="verbose",  help="Prints a lot of info to stderr to aid in debugging")

    (options, args) = parser.parse_args()
    dry_run = options.dry_run
    verbose = options.verbose
  
    atomic_swap = AtomicSwap(float(options.init_amount), float(options.part_amount), options.host, dry_run, verbose)
    atomic_swap.run()
