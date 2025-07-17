from dotenv import load_dotenv
load_dotenv()

import os
import boto3
from cryptography.fernet import Fernet
from eth_account import Account
from datetime import datetime

# DynamoDB setup
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'InkyWallets')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMODB_TABLE)

# Encryption setup
ENCRYPTION_KEY = os.environ['ENCRYPTION_KEY']
fernet = Fernet(ENCRYPTION_KEY.encode())

def create_wallet():
    acct = Account.create()
    private_key = acct.key.hex()
    encrypted_pk = fernet.encrypt(private_key.encode()).decode()
    return acct.address, encrypted_pk

def store_wallet(telegram_id, address, encrypted_private_key):
    table.put_item(Item={
        'telegram_id': telegram_id,
        'address': address,
        'encrypted_private_key': encrypted_private_key,
        'created_at': datetime.utcnow().isoformat()
    })

def get_wallet(telegram_id):
    resp = table.get_item(Key={'telegram_id': telegram_id})
    item = resp.get('Item')
    if item:
        return item['address'], item['encrypted_private_key']
    return None, None

def delete_wallet(telegram_id):
    table.delete_item(Key={'telegram_id': telegram_id})

def decrypt_private_key(encrypted_private_key):
    return fernet.decrypt(encrypted_private_key.encode()).decode() 