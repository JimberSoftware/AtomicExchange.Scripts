from __future__ import print_function
from concurrent import futures
import time
import datetime
import sys
import itertools

import grpc

import atomicswap_pb2
import atomicswap_pb2_grpc

import urllib2

import os
import subprocess
from optparse import OptionParser
import json
from collections import namedtuple

from dry_run import ParticipantDryRun
def _json_object_hook(d): return namedtuple('X', d.keys())(*d.values())
def json2obj(data): return json.loads(data, object_hook=_json_object_hook)



_ONE_DAY_IN_SECONDS = 60 * 60 * 24

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

class AtomicSwap(atomicswap_pb2_grpc.AtomicSwapServicer):

    dry_run = False
    init_amount = 0
    part_amount = 0
    verbose = False

    def verboseLog(self, m):
        if self.verbose:
            eprint('\n['+datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")+']: \n'+m+'\n')

    def __init__(self, init_amount, part_amount, dry_run, verbose):

        self.init_amount = float(init_amount)
        self.part_amount = float(part_amount)

        self.dry_run = dry_run
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

    def ProcessInitiate(self, request, context):

        #######GLOSSARY######
        # init = initiator
        # part = participant
        # ctc = contract
        # tx = transaction
        # addr = address
        #####################
        swapInProgress = True
        self.verboseLog('Step 1: Received Request from Initiator, Confirming amounts and exchanging the recipient addresses')
        self.verboseLog('Expected amounts are:\nInitiator: '+str(self.init_amount)+'\nParticipant: '+str(self.part_amount))
        self.verboseLog('Received amounts are:\nInitiator: '+str(request.init_amount)+'\nParticipant: '+str(request.part_amount))
        if(request.init_amount == self.init_amount and request.part_amount == self.part_amount):
            self.verboseLog('Amounts match.')
        else:
            self.verboseLog('Amounts DO NOT match. Aborting swap.')
            exit(1) # Needs to be handled better

        self.part_addr,_,_ = self.execute('bitcoin-cli getnewaddress \"\" legacy')
        self.part_addr = self.part_addr.rstrip("\r\n")
        self.verboseLog('Generated BTC Address for Initiator to build contract with: ' + self.part_addr)

            # Saving Initiator Address for Contract creation step
        self.init_addr = request.init_addr

            # Print Step info to UI
        print_json(1, "Sent Atomicswap request confirmation with Participant Address", self.step_one_data(request))

        return atomicswap_pb2.InitiateReply(part_addr=self.part_addr)

    def ProcessInitiateSwap(self, request, context):

        self.verboseLog('Step 2: Creating TFT Atomicswap Contract with the Initiator address and the Hashed Secret')

            # Saving Initiator Contract and Transaction hexstrings for Redeem Step
        self.init_ctc_hex = request.init_ctc_hex
        self.init_ctc_tx_hex = request.init_ctc_tx_hex
        self.init_ctc_redeem_addr = request.init_ctc_redeem_addr
        self.verboseLog('Received info from participant: \nInitiator Contract Hex: ' + self.init_ctc_hex + '\n\nInitiator Contract Transaction Hex: ' + self.init_ctc_tx_hex + '\n\nInitiator Contract Redeem Address: ' + self.init_ctc_redeem_addr + '\n\nHashed Secret: ' + request.hashed_secret)

        self.verboseLog('Waiting until the BTC Contract is visible on Explorer, this may take a while...')
        self.waitUntilTxVisible(self.init_ctc_redeem_addr)
        self.verboseLog('A block with the Redeem address was found.')
        self.verboseLog('Auditing Initiator Contract')
        init_ctc_audit_json, _, auditCode = self.execute('btcatomicswap --testnet auditcontract {} {}'.format(self.init_ctc_hex, self.init_ctc_tx_hex))
        self.verboseLog('Audit JSON:\n' + init_ctc_audit_json)
        init_ctc_audit = json2obj(init_ctc_audit_json)
        self.verboseLog('Audit return Code: ' + str(auditCode))

        if auditCode != 0 or float(init_ctc_audit.contractValue[:-4]) != self.init_amount or init_ctc_audit.recipientAddress != self.part_addr:
            self.verboseLog('Audit Failed, aborting swap.')
            return atomicswap_pb2.AcceptSwap(accepted = False)
        else:
            self.verboseLog('Audit completed Successfully')

            # Create Atomicswap Contract on Participant chain using Initiator Address as Redeem Recipient
        part_ctc_json, _, _ = self.execute("tfchainc atomicswap --encoding json -y participate {} {} {}".format(self.init_addr, self.part_amount, request.hashed_secret))
        part_ctc = json2obj(part_ctc_json)
        self.part_ctc_redeem_addr = part_ctc.outputid
        self.verboseLog('Created TFT atomicswap contract:\n' + part_ctc_json)

            # Print Step info to UI
        print_json(2, "Saved Initiator Contract Details and Created Participant Contract", self.step_two_data(request, part_ctc))

            # RPC response to Create Contract request
            # Returns Participant Contract Redeem Address
        self.verboseLog('Sending TFT Contract Redeem Address to Initiator...')
        return atomicswap_pb2.AcceptSwap(part_ctc_redeem_addr=part_ctc.outputid, accepted = True)


    def ProcessRedeemed(self,request,context):

        self.verboseLog('Step 3: Using revealed secret to redeem Initiator Contract')

            # Make Redeem Transaction
        _,_,_ = self.execute("btcatomicswap --testnet --rpcuser=user --rpcpass=pass -s localhost:8332 redeem {} {} {}".format(self.init_ctc_hex, self.init_ctc_tx_hex, request.secret))       
        self.verboseLog('Redeemed BTC contract')

            # Print Step Info to UI
        print_json(3, "Created Redeemed Transaction, Finished Participant Flow", self.step_three_data())

            # RPC response to Redeem Finished message
            # Returns Finished message
        self.verboseLog('Telling Initiator we are finished...')
        global looping
        looping = False
        return atomicswap_pb2.ParticipantRedeemFinished(finished=True)

    def ProcessAbort(self, request, context):
        self.verboseLog('Something went Wrong, Aborting Swap')
        self.refund(87000) # currently 24hrs in seconds
        global looping
        looping = False
        return atomicswap_pb2.Abort(abort=True)


    #########################
    # r = request
    # pc = part_ctc
    #########################

    def step_one_data(self, r):
        data = {}
        data['participantAmount'] = r.part_amount
        data['initiatorAmount'] = r.init_amount
        data['participantAddress'] = self.part_addr
        data['initiatorAddress'] = self.init_addr
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def step_two_data(self, r, pc):
        data = {}
        data['initiatorContractHex'] = r.init_ctc_hex
        data['initiatorContractTransactionHex'] = r.init_ctc_tx_hex
        data['initiatorContractRedeemAddress'] = r.init_ctc_redeem_addr
        data['hashedSecret'] = r.hashed_secret
        data['participantContractRedeemAddress'] = pc.outputid
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def step_three_data(self):
        data = {}
        data['finished'] = "true"
        data['datetime'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    def waitUntilTxVisible(self, hash):

        counter = 0
        spinner = itertools.cycle(['-', '/', '|', '\\'])

        while True:
            sys.stderr.write(spinner.next())
            sys.stderr.flush()
            sys.stderr.write('\b')
            counter += 1
            time.sleep(0.1)
            
            if(counter % 100) == 0:
                try:
                    self.verboseLog('Looking up Redeem Address in explorer:')
                    btc_tx_json = urllib2.urlopen("https://test-insight.bitpay.com/api/addr/"+ hash).read()
                    btc_tx = json2obj(btc_tx_json)
                    self.verboseLog('Redeem address was found in ' + str(btc_tx.txApperances) + ' transactions')
                    if btc_tx.txApperances > 0:
                        break
                    else:
                        self.verboseLog('Waiting 10s')
                except Exception as e:
                    print(e, 'Trying again in 10s...')
                    time.sleep(10)
    
    def refund(self, seconds):
        self.verboseLog('Going to wait a day... until we can refund...')
        time.sleep(seconds)
        part_refund_json,_,refundCode = self.execute('tfchainc atomicswap --encoding json -y refund {}'.format(self.part_ctc_redeem_addr))
        #part_refund = json2obj(part_refund_json)
        self.verboseLog('Refund tx: ' + part_refund_json)
        if refundCode is 0:
            self.verboseLog('Executed refund transaction.')
        else:
            self.verboseLog('Something went wrong with publishing the refund')


def serve(init_amount, part_amount, dry_run, verbose):
    global looping
    global swapInProgress
    retryTimer = 0
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    atomicswap_pb2_grpc.add_AtomicSwapServicer_to_server(AtomicSwap(init_amount, part_amount, dry_run, verbose), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    try:
        while looping:
            time.sleep(5)
            retryTimer += 1
            if (retryTimer > 4 and swapInProgress == False)
                looping = False
    except KeyboardInterrupt:
        server.stop(0)


looping = True
if __name__ == '__main__':
    parser = OptionParser()

    parser.add_option("-m", "--my-amount", dest="part_amount",
                    help="Your amount of your currency to swap", metavar="INITIATORAMOUNT")

    parser.add_option("-o", "--other-amount",
                    dest="init_amount", default=True,
                    help="The amount of the other partners currency to swap")
    
    parser.add_option("-d", "--dry-run", action="store_true",
                        dest="dry_run",  help="Do a dry run with dummy data")

    parser.add_option("-v", "--verbose", action="store_true",
                        dest="verbose",  help="Prints a lot of info to stderr to aid in debugging")

    (options, args) = parser.parse_args()
    
    init_amount = options.init_amount
    part_amount = options.part_amount

    dry_run = options.dry_run
    verbose = options.verbose

    serve(init_amount, part_amount, dry_run, verbose)
