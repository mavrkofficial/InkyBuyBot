# Inky Buy Bot

**Inky Buy Bot** is a Python-based Telegram bot that enables users to buy and sell tokens using ETH on the Ink Layer 2 blockchain. It provides a secure, custodial wallet experience, supports both Uniswap V2 and V3 router contracts for swaps, and is designed for robust, 24/7 operation (e.g., on AWS Lambda).

---

## 🚀 Features

- **Custodial Wallets:** Each Telegram user gets a unique, securely stored wallet.
- **Buy/Sell Tokens:** Swap ETH for tokens and vice versa using InkySwap (V2) and InkyFactory (V3) routers.
- **Fee Routing:** 1% fee on swaps, routed to a designated fee wallet.
- **Explorer Integration:** Token balances and transaction links use the InkOnChain explorer API.
- **Secure Key Management:** Private keys are encrypted at rest and never exposed except to the authenticated user.
- **User-Friendly Telegram UX:** Intuitive command and menu-driven interface.

---

## 🧩 Project Structure

- `bot.py` — Telegram bot logic, user flows, and command handlers.
- `wallet_utils.py` — Wallet creation, encryption, storage, and retrieval.
- `swap_handler.py` — Swap routing, fee management, and contract interaction.
- `config.py` — Network, router, and global constants.
- `UniswapV2Router_ABI.json` — ABI for Uniswap V2 router.
- `SwapRouter02_ABI.json` — ABI for Uniswap V3 router.
- `wallets.db` — SQLite database for wallet storage (auto-created).

---

## 🛠️ How It Works

### 1. Telegram Bot Commands

- `/start` — Initializes a wallet for the user if none exists, displays wallet address and bridge link.
- `/wallet` — Shows wallet address and ETH balance.
- `/export_keys` — Returns the user's decrypted private key (only to the authenticated user).
- `/reset_wallet` — Deletes the old wallet and creates a new one.
- `/buy` — Guides the user through buying a token with ETH.
- `/sell` — Guides the user through selling a token for ETH.
- `/withdraw` — Withdraw ETH or tokens to another address.

All wallet actions are tied to the user's Telegram ID.

### 2. Wallet Security & Storage

- **Wallet Generation:** Uses `eth_account.Account.create()` to generate a new Ethereum wallet.
- **Encryption:** Private keys are encrypted using Fernet (AES-256) from the `cryptography` package.
- **Database:** Wallets are stored in a local SQLite database (`wallets.db`) with the schema:
  ```sql
  CREATE TABLE wallets (
    telegram_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    encrypted_private_key TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
  ```
- **Access Control:** Only the Telegram user who owns a wallet can access its private key.
- **Key Management:** The encryption key is loaded from an environment variable and never hardcoded.

### 3. Swap Routing & Execution

#### Supported Routers

- **UniswapV2Router (InkySwap):**
  - Used for V2 liquidity pairs on InkySwap.com.
  - Contract call: `swapExactETHForTokens` (buy), `swapExactTokensForETH` (sell).
  - Path: `[WETH, token]` for buys, `[token, WETH]` for sells.
  - Requires token approval for sells.

- **SwapRouter02 (InkyFactory):**
  - Used for V3 liquidity pairs deployed via InkyFactory.com.
  - Contract call: `exactInputSingle` for both buy and sell.
  - For buys: ETH is wrapped to WETH automatically.
  - For sells: WETH is unwrapped to ETH after the swap using the `withdraw` function.

#### Router Selection Logic

- The bot checks for available pools/pairs:
  - For V3: Calls `getPool(tokenIn, tokenOut, fee)` on the factory.
  - For V2: Calls `getPair(tokenA, tokenB)` on the factory.
- The first router with a valid pool/pair is used for the swap.

#### Fee Handling

- **1% Fee:** On every swap, 1% of the ETH value is sent to a designated fee wallet.
  - For buys: Fee is deducted from the ETH sent, and the remainder is swapped.
  - For sells: After the swap, 1% of the received ETH is sent to the fee wallet, and the remainder is returned to the user.
- **Fee Transactions:** All fee transfers are signed and sent from the user's wallet.

### 4. Explorer API Usage

- **Token Balances:** The bot fetches token balances using:
  ```
  https://explorer.inkonchain.com/api/v2/addresses/{user_address}/token-balances
  ```
- **Transaction Links:** All transaction confirmations include a link to the InkOnChain explorer:
  ```
  https://explorer.inkonchain.com/tx/{tx_hash}
  ```

### 5. Security Practices

- **Private Key Encryption:** All private keys are encrypted at rest using Fernet (AES-256).
- **Access Control:** Only the Telegram user who owns a wallet can access its private key.
- **No Sensitive Data in Code:** No private keys, sensitive API keys, or .env secrets are included in the codebase or documentation.
- **Error Handling:** The bot gracefully handles errors such as unsupported tokens, failed swaps, and RPC timeouts.

---

## 🏗️ Deployment

- The bot is designed to run as a long-lived process (e.g., on AWS Lambda, EC2, or any server).
- All configuration (RPC URL, chain ID, fee wallet, encryption key, bot token) is loaded from environment variables.
- **Do not** commit your `.env` file or any sensitive keys to version control.

---

## 📝 Router Contract Differences

| Feature         | UniswapV2Router (InkySwap)         | SwapRouter02 (InkyFactory)         |
|-----------------|------------------------------------|------------------------------------|
| Type            | V2                                 | V3                                 |
| Buy Call        | `swapExactETHForTokens`            | `exactInputSingle`                 |
| Sell Call       | `swapExactTokensForETH`            | `exactInputSingle` + `withdraw`    |
| Path            | `[WETH, token]`                    | params object (tokenIn, tokenOut)  |
| Pool Discovery  | `getPair(tokenA, tokenB)`          | `getPool(tokenIn, tokenOut, fee)`  |
| Approval Needed | Yes (for sells)                    | Yes (for sells)                    |
| WETH Handling   | Manual for V2, auto for V3         | Auto-wrap/unwrap in logic          |

---

## 🧑‍💻 Development & Extensibility

- **Modular Design:** Each major function (wallet, swap, config) is in its own file for easy upgrades.
- **ABIs:** Router ABIs are loaded from JSON files and selected dynamically based on router type.
- **Logging:** All user actions are logged to `bot.log` for audit and debugging.

---

## 🧪 Example User Flow

```
/start
🦑 Welcome to Inky Buy Bot!
👛 Your wallet: 0xAbc...123
🌉 Bridge ETH to Ink: https://inkonchain.com/bridge

/buy
🛒 Buy Tokens
🔗 Enter the token address you want to buy:
User: 0xToken...

💰 Your ETH balance: 1.000000 ETH
How much ETH do you want to swap?
User: 1

🛒 Swap Summary
• Amount: 1.0000 ETH
• Token: 0xToken...
• Fee: 0.0100 ETH
Do you want to proceed?
[✅ Confirm] [❌ Cancel]

✅ Success!
View on Explorer: https://explorer.inkonchain.com/tx/0x...
```

---

## 🛡️ Security Checklist

- [x] Private keys are always encrypted at rest.
- [x] Only the authenticated Telegram user can access their private key.
- [x] No sensitive data is exposed in code or documentation.
- [x] All swap and fee transactions are signed by the user's wallet.
- [x] Error handling for all major operations.

---

## 🏁 Running the Bot

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Set up your environment variables (see `.env.template` for required keys).
3. Run the bot:
   ```
   python bot.py
   ```

---

## ❗️ Notes

- **Never share your `.env` file, private keys, or sensitive configuration.**
- **This bot is for educational and operational use on Ink Layer 2.**
- **All swaps are routed to the best available pool (V2 or V3) automatically.**

---

For further questions, see the code comments or contact the project maintainer.

--- 