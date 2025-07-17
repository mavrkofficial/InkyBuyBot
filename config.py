import os
from dotenv import load_dotenv

load_dotenv()

# Routers
ROUTERS = [
    {
        "name": "InkyFactory",
        "router": "0x177778F19E89dD1012BdBe603F144088A95C4B53",
        "factory": "0x640887A9ba3A9C53Ed27D0F7e8246A4F933f3424",
        "type": "v3",
        "fee": 10000,
        "weth": "0x4200000000000000000000000000000000000006"
    },
    {
        "name": "InkySwap",
        "router": "0xA8C1C38FF57428e5C3a34E0899Be5Cb385476507",
        "factory": "0x458C5d5B75ccBA22651D2C5b61cB1EA1e0b0f95D",
        "type": "v2",
        "weth": "0x4200000000000000000000000000000000000006"
    }
]

# Network
RPC_URL = os.getenv("RPC_URL", "https://ink.drpc.org")
CHAIN_ID = int(os.getenv("CHAIN_ID", 57073))
EXPLORER_URL = "https://explorer.inkonchain.com"
BRIDGE_URL = "https://inkonchain.com/bridge"

# Fee wallet
FEE_WALLET = os.getenv("FEE_WALLET", "0x557bf05A32fc154203C54D9a16b7382AE3ab527a")

# Encryption key
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Telegram bot token
BOT_TOKEN = os.getenv("BOT_TOKEN") 