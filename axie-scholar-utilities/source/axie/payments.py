import sys
import json
import logging
from datetime import datetime, timedelta
from time import sleep

from jsonschema import validate
from jsonschema.exceptions import ValidationError
from web3 import Web3, exceptions

from axie.schemas import payments_schema, payments_percent_schema
from axie.utils import (
    check_balance,
    get_nonce,
    load_json,
    Singleton,
    ImportantLogsFilter,
    SLP_CONTRACT,
    RONIN_PROVIDER_FREE,
    TIMEOUT_MINS,
    USER_AGENT
)


CREATOR_FEE_ADDRESS = "ronin:9fa1bc784c665e683597d3f29375e45786617550"

now = int(datetime.now().timestamp())
log_file = f'logs/results_{now}.log'
logger = logging.getLogger()
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.addFilter(ImportantLogsFilter())
logger.addHandler(file_handler)


class Payment:
    def __init__(self, name, payment_type, from_acc, from_private, to_acc, amount, summary):
        self.w3 = Web3(
            Web3.HTTPProvider(
                RONIN_PROVIDER_FREE,
                request_kwargs={"headers": {"content-type": "application/json", "user-agent": USER_AGENT}}))
        self.name = name
        self.payment_type = payment_type
        self.from_acc = from_acc.replace("ronin:", "0x")
        self.from_private = from_private
        self.to_acc = to_acc.replace("ronin:", "0x")
        self.amount = amount
        self.summary = summary
        with open("axie/slp_abi.json", encoding='utf-8') as f:
            slb_abi = json.load(f)
        self.contract = self.w3.eth.contract(
            address=Web3.toChecksumAddress(SLP_CONTRACT),
            abi=slb_abi
        )

    def send_replacement_tx(self, nonce):
        # check nonce is still available, do nothing if nonce is not available anymore
        if nonce != get_nonce(self.from_acc):
            return
        # build replacement tx
        replacement_tx = self.contract.functions.transfer(
            Web3.toChecksumAddress(self.from_acc),
            0
        ).buildTransaction({
            "chainId": 2020,
            "gas": 492874,
            "gasPrice": self.w3.toWei("0", "gwei"),
            "nonce": nonce
        })
        # Sign Transaction
        signed = self.w3.eth.account.sign_transaction(
            replacement_tx,
            private_key=self.from_private
        )
        # Send raw transaction
        self.w3.eth.send_raw_transaction(signed.rawTransaction)
        # get transaction hash
        new_hash = self.w3.toHex(self.w3.keccak(signed.rawTransaction))
        # Wait for transaction to finish or timeout
        start_time = datetime.now()
        while True:
            # We will wait for max 5min for this replacement tx to happen
            if datetime.now() - start_time > timedelta(minutes=TIMEOUT_MINS):
                success = False
                logging.info("Replacement transaction, timed out!")
                break
            try:
                receipt = self.w3.eth.get_transaction_receipt(new_hash)
                if receipt['status'] == 1:
                    success = True
                else:
                    success = False
                break
            except exceptions.TransactionNotFound:
                sleep(10)
                logging.info(f"Waiting for replacement tx to finish (Nonce: {nonce})")

        if success:
            logging.info(f"Successfuly replaced transaction with nonce: {nonce}")
            logging.info(f"Trying again to execute transaction {self} in 10 seconds")
            sleep(10)
            self.execute()
        else:
            logging.info(f"Important: Replacement transaction failed. Means we could not complete tx {self}")
            logging.info(f"Important: Please fix account ({self.name}) transactions manually before launching again.")

    def execute(self):
        # Get Nonce
        nonce = get_nonce(self.from_acc)
        # Build transaction
        transaction = self.contract.functions.transfer(
            Web3.toChecksumAddress(self.to_acc),
            self.amount
        ).buildTransaction({
            "chainId": 2020,
            "gas": 246437,
            "gasPrice": self.w3.toWei("0", "gwei"),
            "nonce": nonce
        })
        # Sign Transaction
        signed = self.w3.eth.account.sign_transaction(
            transaction,
            private_key=self.from_private
        )
        # Send raw transaction
        self.w3.eth.send_raw_transaction(signed.rawTransaction)
        # get transaction hash
        hash = self.w3.toHex(self.w3.keccak(signed.rawTransaction))
        # Wait for transaction to finish or timeout
        start_time = datetime.now()

        while True:
            # We will wait for max 10minutes for this tx to respond, if it does not, we will re-try
            if datetime.now() - start_time > timedelta(minutes=TIMEOUT_MINS):
                success = False
                logging.info(f"Transaction {self}, timed out!")
                break
            try:
                recepit = self.w3.eth.get_transaction_receipt(hash)
                if recepit["status"] == 1:
                    success = True
                else:
                    success = False
                break
            except exceptions.TransactionNotFound:
                # Sleep 10s while waiting
                sleep(10)
                logging.info(f"Waiting for transaction '{self}' to finish (Nonce:{nonce})...")

        if success:
            logging.info(f"Important: Transaction {self} completed! Hash: {hash} - "
                         f"Explorer: https://explorer.roninchain.com/tx/{str(hash)}")
            self.summary.increase_payout(
                amount=self.amount,
                address=self.to_acc.replace('0x', 'ronin:'),
                payout_type=self.payment_type)
        else:
            logging.info(f"Important: Transaction {self} failed. Trying to replace it with a 0 value tx and re-try.")
            self.send_replacement_tx(nonce)

    def __str__(self):
        return f"{self.name}({self.to_acc.replace('0x', 'ronin:')}) for the amount of {self.amount} SLP"


class AxiePaymentsManager:
    def __init__(self, payments_file, secrets_file, auto=False):
        self.payments_file = load_json(payments_file)
        self.secrets_file = load_json(secrets_file)
        self.manager_acc = None
        self.scholar_accounts = None
        self.donations = None
        self.type = None
        self.auto = auto
        self.summary = PaymentsSummary()

    def verify_inputs(self):
        logging.info("Validating file inputs...")
        validation_success = True
        # Validate payments file
        amount_msg = None
        percent_msg = None
        try:
            validate(self.payments_file, payments_schema)
            self.type = "amount"
        except ValidationError as ex:
            amount_msg = ("If you were tyring to pay using amounts:\n"
                          f"Error given: {ex.message}\n"
                          f"For attribute in: {list(ex.path)}\n")
            validation_success = False
        if not self.type:
            try:
                validate(self.payments_file, payments_percent_schema)
                self.type = "percent"
                validation_success = True
            except ValidationError as ex:
                percent_msg = ("If you were tyring to pay using percents:\n"
                               f"Error given: {ex.message}\n"
                               f"For attribute in: {list(ex.path)}\n")
                validation_success = False
        if not validation_success:
            msg = "Payments file failed validation. Please review it.\n"
            if amount_msg:
                msg += amount_msg
            if percent_msg:
                msg += percent_msg
            logging.critical(msg)
        if len(self.payments_file["Manager"].replace("ronin:", "0x")) != 42:
            logging.critical(f"Check your manager ronin {self.payments_file['Manager']}, it has an incorrect format")
            validation_success = False
        # check donations do not exceed 100%
        if self.payments_file.get("Donations") and self.type == "amount":
            total = sum([x["Percent"] for x in self.payments_file.get("Donations")])
            if total > 0.99:
                logging.critical("Payments file donations exeeds 100%, please review it")
                validation_success = False
            if any(len(dono['AccountAddress'].replace("ronin:", "0x")) != 42 for dono in self.payments_file["Donations"]):  # noqa
                logging.critical("Please review the ronins in your donations. One or more are wrong!")
                validation_success = False
            self.donations = self.payments_file["Donations"]
        if self.payments_file.get("Donations") and self.type == "percent":
            total = sum([x["Percent"] for x in self.payments_file.get("Donations")])
            if total > 99:
                logging.critical("Payments file donations exeeds 100%, please review it")
                validation_success = False
            if any(len(dono['AccountAddress'].replace("ronin:", "0x")) != 42 for dono in self.payments_file["Donations"]): # noqa
                logging.critical("Please review the ronins in your donations. One or more are wrong!")
                validation_success = False
            self.donations = self.payments_file["Donations"]

        # Check we have private keys for all accounts
        for acc in self.payments_file["Scholars"]:
            if acc["AccountAddress"] not in self.secrets_file:
                logging.critical(f"Account '{acc['Name']}' is not present in secret file, please add it.")
                validation_success = False
        for sf in self.secrets_file:
            if len(self.secrets_file[sf]) != 66 or self.secrets_file[sf][:2] != "0x":
                logging.critical(f"Private key for account {sf} is not valid, please review it!")
                validation_success = False
        if not validation_success:
            logging.critical("Please make sure your payments.json file looks like the one in the README.md\n"
                             "Find it here: https://ferranmarin.github.io/axie-scholar-utilities/")
            logging.critical("If your problem is with secrets.json, "
                             "delete it and re-generate the file starting with an empty secrets file.")
            sys.exit()
        self.manager_acc = self.payments_file["Manager"]
        self.scholar_accounts = self.payments_file["Scholars"]
        logging.info("Files correctly validated!")

    def check_acc_has_enough_balance(self, account, balance):
        account_balance = check_balance(account)
        if account_balance < balance:
            logging.critical(f"Balance in account {account} is "
                             "inssuficient to cover all planned payments!")
            return False
        elif account_balance - balance > 0:
            logging.info(f'These payments will leave {account_balance - balance} SLP in your wallet.'
                         'Cancel payments and adjust payments if you want to leave 0 SLP in it.')
        return True

    def prepare_payout(self):
        if self.type == "amount":
            self.prepare_payout_amount()
        elif self.type == "percent":
            self.prepare_payout_percent()
        else:
            logging.critical(f"Unexpected error! Unrecognized payments mode {self.type}")

    def prepare_payout_amount(self):
        for acc in self.scholar_accounts:
            fee = 0
            total_dono = 0
            total_payments = 0
            acc_payments = []
            # scholar_payment
            acc_payments.append(Payment(
                f"Payment to scholar of {acc['Name']}",
                "scholar",
                acc["AccountAddress"],
                self.secrets_file[acc["AccountAddress"]],
                acc["ScholarPayoutAddress"],
                acc["ScholarPayout"],
                self.summary
            ))
            fee += acc["ScholarPayout"]
            total_payments += acc["ScholarPayout"]
            if acc.get("TrainerPayoutAddress"):
                # trainer_payment
                acc_payments.append(Payment(
                    f"Payment to trainer of {acc['Name']}",
                    "trainer",
                    acc["AccountAddress"],
                    self.secrets_file[acc["AccountAddress"]],
                    acc["TrainerPayoutAddress"],
                    acc["TrainerPayout"],
                    self.summary
                ))
                fee += acc["TrainerPayout"]
                total_payments += acc["TrainerPayout"]
            manager_payout = acc["ManagerPayout"]
            fee += manager_payout
            total_payments += acc["ManagerPayout"]
            if self.donations:
                for dono in self.donations:
                    amount = round(manager_payout * dono["Percent"])
                    if amount > 1:
                        total_dono += amount
                        # donation payment
                        acc_payments.append(Payment(
                            f"Donation to {dono['Name']} for {acc['Name']}",
                            "donation",
                            acc["AccountAddress"],
                            self.secrets_file[acc["AccountAddress"]],
                            dono["AccountAddress"],
                            amount,
                            self.summary
                        ))
            # Creator fee
            fee_payout = round(fee * 0.01)
            if fee_payout > 1:
                total_dono += fee_payout
                acc_payments.append(Payment(
                            f"Donation to software creator for {acc['Name']}",
                            "donation",
                            acc["AccountAddress"],
                            self.secrets_file[acc["AccountAddress"]],
                            CREATOR_FEE_ADDRESS,
                            fee_payout,
                            self.summary
                        ))
            # manager payment
            acc_payments.append(Payment(
                f"Payment to manager of {acc['Name']}",
                "manager",
                acc["AccountAddress"],
                self.secrets_file[acc["AccountAddress"]],
                self.manager_acc,
                manager_payout - total_dono,
                self.summary
            ))
            if not self.check_acc_has_enough_balance(acc["AccountAddress"], total_payments):
                logging.info(f"Important: Skipping payments for account '{acc['Name']}'. "
                             "Insufficient funds!")
            else:
                if manager_payout - total_dono >= 0:
                    self.payout_account(acc["Name"], acc_payments)
                else:
                    logging.info("Fix your payments, currently after fees and donations manager "
                                 f"is receiving a negative payment of {manager_payout - total_dono}")
        logging.info(f"Important: Transactions Summary:\n {self.summary}")

    def prepare_payout_percent(self):
        for acc in self.scholar_accounts:
            acc_balance = check_balance(acc['AccountAddress'])
            total_payments = 0
            acc_payments = []
            # Scholar Payment
            scholar_amount = acc_balance * (acc["ScholarPercent"]/100)
            scholar_amount += acc.get("ScholarPayout", 0)
            scholar_amount -= acc.get("Penalty", 0)
            scholar_amount = round(scholar_amount)

            if scholar_amount > 0:
                acc_payments.append(Payment(
                    f"Payment to scholar of {acc['Name']}",
                    "scholar",
                    acc["AccountAddress"],
                    self.secrets_file[acc["AccountAddress"]],
                    acc["ScholarPayoutAddress"],
                    scholar_amount,
                    self.summary
                ))
                total_payments += scholar_amount
            if acc.get("TrainerPayoutAddress"):
                # Trainer Payment
                trainer_amount = acc_balance * (acc["TrainerPercent"]/100)
                trainer_amount += acc.get("TrainerPayout", 0)
                trainer_amount = round(trainer_amount)
                if trainer_amount > 0:
                    acc_payments.append(Payment(
                        f"Payment to trainer of {acc['Name']}",
                        "trainer",
                        acc["AccountAddress"],
                        self.secrets_file[acc["AccountAddress"]],
                        acc["TrainerPayoutAddress"],
                        trainer_amount,
                        self.summary
                    ))
                    total_payments += trainer_amount
            manager_payout = acc_balance - total_payments
            if self.donations:
                # Extra Donations
                for dono in self.donations:
                    dono_amount = round(manager_payout * (dono["Percent"]/100))
                    if dono_amount > 1:
                        acc_payments.append(Payment(
                                f"Donation to {dono['Name']} for {acc['Name']}",
                                "donation",
                                acc["AccountAddress"],
                                self.secrets_file[acc["AccountAddress"]],
                                dono["AccountAddress"],
                                dono_amount,
                                self.summary
                            ))
                        manager_payout -= dono_amount
                        total_payments += dono_amount
            # Fee Payments
            # fee_amount = round(acc_balance * 0.01)

            fee_amount = -1

            if fee_amount > 0:
                acc_payments.append(Payment(
                            f"Donation to software creator for {acc['Name']}",
                            "donation",
                            acc["AccountAddress"],
                            self.secrets_file[acc["AccountAddress"]],
                            CREATOR_FEE_ADDRESS,
                            fee_amount,
                            self.summary
                        ))
                manager_payout -= fee_amount
                total_payments += fee_amount
            # Manager Payment
            if manager_payout > 0:
                acc_payments.append(Payment(
                    f"Payment to manager of {acc['Name']}",
                    "manager",
                    acc["AccountAddress"],
                    self.secrets_file[acc["AccountAddress"]],
                    self.manager_acc,
                    manager_payout,
                    self.summary
                ))
                total_payments += manager_payout
            else:
                logging.info("Important: Skipping manager payout as it resulted in 0 SLP.")
            if self.check_acc_has_enough_balance(acc['AccountAddress'], total_payments) and acc_balance > 0:
                self.payout_account(acc['Name'], acc_payments)
            else:
                logging.info(f"Important: Skipping payments for account '{acc['Name']}'. "
                             "Insufficient funds!")
        logging.info(f"Important: Transactions Summary:\n {self.summary}")

    def payout_account(self, acc_name, payment_list):
        logging.info(f"Payments for {acc_name}:")
        logging.info(",\n".join(str(p) for p in payment_list))
        accept = "y" if self.auto else None
        while accept not in ["y", "n", "Y", "N"]:
            accept = input("Do you want to proceed with these transactions?(y/n): ")
        if accept.lower() == "y":
            for p in payment_list:
                p.execute()
            logging.info(f"Transactions completed for account: '{acc_name}'")
        else:
            logging.info(f"Transactions canceled for account: '{acc_name}'")


class PaymentsSummary(Singleton):

    def __init__(self):
        self.manager = {"accounts": [], "slp": 0}
        self.trainer = {"accounts": [], "slp": 0}
        self.scholar = {"accounts": [], "slp": 0}
        self.donations = {"accounts": [], "slp": 0}

    def increase_payout(self, amount, address, payout_type):
        if payout_type == "manager":
            self.increase_manager_payout(amount, address)
        elif payout_type == "scholar":
            self.increase_scholar_payout(amount, address)
        elif payout_type == "donation":
            self.increase_donations_payout(amount, address)
        elif payout_type == "trainer":
            self.increase_trainer_payout(amount, address)

    def increase_manager_payout(self, amount, address):
        self.manager["slp"] += amount
        if address not in self.manager["accounts"]:
            self.manager["accounts"].append(address)

    def increase_trainer_payout(self, amount, address):
        self.trainer["slp"] += amount
        if address not in self.trainer["accounts"]:
            self.trainer["accounts"].append(address)

    def increase_scholar_payout(self, amount, address):
        self.scholar["slp"] += amount
        if address not in self.scholar["accounts"]:
            self.scholar["accounts"].append(address)

    def increase_donations_payout(self, amount, address):
        self.donations["slp"] += amount
        if address not in self.donations["accounts"]:
            self.donations["accounts"].append(address)

    def __str__(self):
        msg = "No payments made!"
        if self.manager["accounts"] and self.scholar["accounts"]:
            msg = f'Paid {len(self.manager["accounts"])} managers, {self.manager["slp"]} SLP.\n'
            msg += f'Paid {len(self.scholar["accounts"])} scholars, {self.scholar["slp"]} SLP.\n'
        if self.manager["accounts"] and not self.scholar["accounts"]:
            msg = f'Paid {len(self.manager["accounts"])} managers, {self.manager["slp"]} SLP.\n'
        if self.scholar["accounts"] and not self.manager["accounts"]:
            msg = f'Paid {len(self.scholar["accounts"])} scholars, {self.scholar["slp"]} SLP.\n'
        if self.trainer["slp"] > 0:
            msg += f'Paid {len(self.trainer["accounts"])} trainers, {self.trainer["slp"]} SLP.\n'
        if self.donations["slp"] > 0:
            msg += f'Donated to {len(self.donations["accounts"])} organisations, {self.donations["slp"]} SLP.\n'
        return msg
