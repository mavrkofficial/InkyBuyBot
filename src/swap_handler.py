import json
from web3 import Web3
from eth_account import Account
from config import ROUTERS, FEE_WALLET, RPC_URL, CHAIN_ID
import time

w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Load ABIs
V2_ABI = None
V3_ABI = None
V2_FACTORY_ABI = [
    {"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}], "name": "getPair", "outputs": [{"internalType": "address", "name": "pair", "type": "address"}], "stateMutability": "view", "type": "function"}
]
V3_FACTORY_ABI = [
    {"inputs": [{"internalType": "address", "name": "tokenA", "type": "address"}, {"internalType": "address", "name": "tokenB", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"internalType": "address", "name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}
]
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "type": "function",
    },
]

def load_abi(file_name):
    with open(file_name) as f:
        return json.load(f)

if V2_ABI is None:
    V2_ABI = load_abi('UniswapV2Router_ABI.json')
if V3_ABI is None:
    V3_ABI = load_abi('SwapRouter02_ABI.json')

def select_router(token_in, token_out):
    """
    Returns (router_dict, abi, router_type) for the first router that supports the pair.
    """
    # Ensure all addresses are checksum format
    token_in = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)
    for router in ROUTERS:
        if router['type'] == 'v3':
            factory = w3.eth.contract(address=Web3.to_checksum_address(router['factory']), abi=V3_FACTORY_ABI)
            pool = factory.functions.getPool(token_in, token_out, router['fee']).call()
            # Check if pool address is non-zero
            if pool != "0x0000000000000000000000000000000000000000":
                return router, V3_ABI, 'v3'
        elif router['type'] == 'v2':
            factory = w3.eth.contract(address=Web3.to_checksum_address(router['factory']), abi=V2_FACTORY_ABI)
            pair = factory.functions.getPair(token_in, token_out).call()
            # Check if pair address is non-zero
            if pair != "0x0000000000000000000000000000000000000000":
                return router, V2_ABI, 'v2'
    return None, None, None

def calculate_fee(amount):
    return int(amount * 0.01)

def send_fee_and_return(user_address, user_private_key, fee_amount, return_amount, gas_price=None):
    # Send fee to FEE_WALLET
    nonce = w3.eth.get_transaction_count(user_address)
    tx_fee = {
        'to': Web3.to_checksum_address(FEE_WALLET),
        'value': fee_amount,
        'gas': 30000,
        'gasPrice': gas_price or w3.eth.gas_price,
        'nonce': nonce,
        'chainId': CHAIN_ID
    }
    signed_fee = w3.eth.account.sign_transaction(tx_fee, user_private_key)
    tx_fee_hash = w3.eth.send_raw_transaction(signed_fee.raw_transaction)

    # Send remainder to user
    nonce_for_return = w3.eth.get_transaction_count(user_address)
    tx_return = {
        'to': Web3.to_checksum_address(user_address),
        'value': return_amount,
        'gas': 30000,
        'gasPrice': gas_price or w3.eth.gas_price,
        'nonce': nonce_for_return,
        'chainId': CHAIN_ID
    }
    signed_return = w3.eth.account.sign_transaction(tx_return, user_private_key)
    tx_return_hash = w3.eth.send_raw_transaction(signed_return.raw_transaction)
    return tx_fee_hash.hex(), tx_return_hash.hex()

def execute_buy(user_address, user_private_key, eth_amount, token_out):
    """
    Executes a buy (ETH -> token_out) for the user. Returns tx hash or error.
    """
    try:
        fee = calculate_fee(eth_amount)
        swap_amount = eth_amount - fee
        # Use 2x gas price for all txs
        fast_gas_price = int(w3.eth.gas_price * 2)

        # Send fee first
        nonce_fee = w3.eth.get_transaction_count(user_address)
        tx_fee = {
            'to': Web3.to_checksum_address(FEE_WALLET),
            'value': fee,
            'gas': 30000,  # slightly higher than 21000 for safety
            'gasPrice': fast_gas_price,
            'nonce': nonce_fee,
            'chainId': CHAIN_ID
        }
        signed_fee = w3.eth.account.sign_transaction(tx_fee, user_private_key)
        fee_tx_hash = w3.eth.send_raw_transaction(signed_fee.raw_transaction)
        w3.eth.wait_for_transaction_receipt(fee_tx_hash)

        # Router selection
        weth = ROUTERS[0]['weth'] # Assuming weth is consistent across routers
        router, abi, router_type = select_router(weth, token_out)
        if not router:
            return {'error': 'No supported pool/pair for this token.'}
        
        router_contract = w3.eth.contract(address=Web3.to_checksum_address(router['router']), abi=abi)
        deadline = int(time.time()) + 300

        # Fetch nonce again for the swap transaction
        nonce_swap = w3.eth.get_transaction_count(user_address)

        if router_type == 'v3':
            # V3: exactInputSingle
            params = {
                'tokenIn': Web3.to_checksum_address(weth),
                'tokenOut': Web3.to_checksum_address(token_out),
                'fee': router['fee'],
                'recipient': Web3.to_checksum_address(user_address),
                'amountIn': swap_amount,
                'amountOutMinimum': 0, # Consider setting a small slippage tolerance
                'sqrtPriceLimitX96': 0
            }
            tx = router_contract.functions.exactInputSingle(params).build_transaction({
                'from': Web3.to_checksum_address(user_address),
                'value': swap_amount,
                'gas': 600000, # was 400000
                'gasPrice': fast_gas_price,
                'nonce': nonce_swap,
                'chainId': CHAIN_ID
            })
        else: # router_type == 'v2'
            # V2: swapExactETHForTokens
            path = [Web3.to_checksum_address(weth), Web3.to_checksum_address(token_out)]
            tx = router_contract.functions.swapExactETHForTokens(
                0, # amountOutMin (slippage tolerance)
                path,
                Web3.to_checksum_address(user_address),
                deadline
            ).build_transaction({
                'from': Web3.to_checksum_address(user_address),
                'value': swap_amount,
                'gas': 600000, # was 400000
                'gasPrice': fast_gas_price,
                'nonce': nonce_swap,
                'chainId': CHAIN_ID
            })
        
        signed_tx = w3.eth.account.sign_transaction(tx, user_private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        return {'tx_hash': tx_hash.hex()}
    except Exception as e:
        if 'nonce too low' in str(e):
            return {'error': 'A previous transaction is still pending. Please wait for it to confirm before making another trade.'}
        return {'error': str(e)}

def execute_sell(user_address, user_private_key, token_in, amount_in):
    """
    Executes a sell (token_in -> ETH) for the user. Returns tx hash or error.
    """
    try:
        weth = ROUTERS[0]['weth'] # Assuming weth is consistent across routers
        router, abi, router_type = select_router(token_in, weth)
        if not router:
            return {'error': 'No supported pool/pair for this token.'}
        
        router_contract = w3.eth.contract(address=Web3.to_checksum_address(router['router']), abi=abi)
        deadline = int(time.time()) + 300
        # Use 2x gas price for all txs
        fast_gas_price = int(w3.eth.gas_price * 2)
        
        # Approve router to spend token_in
        token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_in), abi=ERC20_ABI)
        
        nonce_approve = w3.eth.get_transaction_count(user_address)
        approve_tx = token_contract.functions.approve(Web3.to_checksum_address(router['router']), amount_in).build_transaction({
            'from': Web3.to_checksum_address(user_address),
            'gas': 80000, # was 60000
            'gasPrice': fast_gas_price,
            'nonce': nonce_approve,
            'chainId': CHAIN_ID
        })
        signed_approve = w3.eth.account.sign_transaction(approve_tx, user_private_key)
        approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        w3.eth.wait_for_transaction_receipt(approve_hash)

        # Fetch nonce again for the swap transaction
        nonce_swap = w3.eth.get_transaction_count(user_address)

        if router_type == 'v3':
            eth_balance_before = w3.eth.get_balance(user_address) # Get balance before swap and unwrap

            params = {
                'tokenIn': Web3.to_checksum_address(token_in),
                'tokenOut': Web3.to_checksum_address(weth),
                'fee': router['fee'],
                'recipient': Web3.to_checksum_address(user_address),
                'amountIn': amount_in,
                'amountOutMinimum': 0, # Consider setting a small slippage tolerance
                'sqrtPriceLimitX96': 0
            }
            tx = router_contract.functions.exactInputSingle(params).build_transaction({
                'from': Web3.to_checksum_address(user_address),
                'gas': 600000, # was 400000
                'gasPrice': fast_gas_price,
                'nonce': nonce_swap,
                'chainId': CHAIN_ID
            })
            signed_tx = w3.eth.account.sign_transaction(tx, user_private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash)

            try:
                # Unwrap WETH to ETH
                weth_contract = w3.eth.contract(address=Web3.to_checksum_address(weth), abi=ERC20_ABI)
                weth_balance = weth_contract.functions.balanceOf(Web3.to_checksum_address(user_address)).call()
                
                if weth_balance > 0:
                    nonce_unwrap = w3.eth.get_transaction_count(user_address)
                    unwrap_tx = weth_contract.functions.withdraw(weth_balance).build_transaction({
                        'from': Web3.to_checksum_address(user_address),
                        'gas': 80000, # was 60000
                        'gasPrice': fast_gas_price,
                        'nonce': nonce_unwrap,
                        'chainId': CHAIN_ID
                    })
                    signed_unwrap = w3.eth.account.sign_transaction(unwrap_tx, user_private_key)
                    unwrap_hash = w3.eth.send_raw_transaction(signed_unwrap.raw_transaction)
                    w3.eth.wait_for_transaction_receipt(unwrap_hash)
                else:
                    unwrap_hash = None # No WETH to unwrap

                eth_balance_after = w3.eth.get_balance(user_address)
                eth_delta = eth_balance_after - eth_balance_before
                
                if eth_delta > 0: # Only apply fee if ETH was received
                    fee = calculate_fee(eth_delta)
                    return_amount = eth_delta - fee
                    fee_hash, return_hash = send_fee_and_return(user_address, user_private_key, fee, return_amount, fast_gas_price)
                    print(f"Fee tx hash: {fee_hash}, Return tx hash: {return_hash}")
                    return {'tx_hash': tx_hash.hex(), 'unwrap_hash': (unwrap_hash.hex() if unwrap_hash else None), 'fee_hash': fee_hash, 'return_hash': return_hash}
                else:
                    return {'tx_hash': tx_hash.hex(), 'unwrap_hash': (unwrap_hash.hex() if unwrap_hash else None), 'fee_error': 'No ETH received from swap or unwrap, fee not applied.'}
            except Exception as unwrap_e:
                return {'tx_hash': tx_hash.hex(), 'unwrap_error': str(unwrap_e)}
        else: # router_type == 'v2'
            # V2: swapExactTokensForETH
            path = [Web3.to_checksum_address(token_in), Web3.to_checksum_address(weth)]
            tx = router_contract.functions.swapExactTokensForETH(
                amount_in,
                0, # amountOutMin (slippage tolerance)
                path,
                Web3.to_checksum_address(user_address),
                deadline
            ).build_transaction({
                'from': Web3.to_checksum_address(user_address),
                'gas': 600000, # was 400000
                'gasPrice': fast_gas_price,
                'nonce': nonce_swap,
                'chainId': CHAIN_ID
            })
            signed_tx = w3.eth.account.sign_transaction(tx, user_private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash)
            return {'tx_hash': tx_hash.hex()}
    except Exception as e:
        if 'nonce too low' in str(e):
            return {'error': 'A previous transaction is still pending. Please wait for it to confirm before making another trade.'}
        return {'error': str(e)}