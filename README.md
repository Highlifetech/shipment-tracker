# 📦 Lark Shipment Tracking Bot

Automatically tracks shipments from your Lark Sheets, updates status & delivery dates, and sends daily summaries to a Lark group chat.

## How It Works

```
Every 6 hours (GitHub Actions):
  1. Reads tracking numbers from your Lark Sheets
  2. Looks up status via FedEx/UPS/USPS/DHL free APIs
  3. Updates Status (col M) and Delivery Date (col Q) in the sheet
  4. Sends a summary message to your Lark group chat
```

## Setup Guide (Step by Step)

### Step 1: Create a Lark Custom App

1. Go to [Lark Developer Console](https://open.larksuite.com/) 
   - For JP region: https://open.jp.larksuite.com/
2. Click **Create Custom App**
3. Name it "Shipment Tracker" (or anything you like)
4. Under **Permissions & Scopes**, add:
   - `sheets:spreadsheet` (read/write sheets)
   - `im:message:send_as_bot` (send messages)
5. Under **App Release**, publish the app to your organization
6. Copy your **App ID** and **App Secret**

### Step 2: Add Bot to Group Chat

1. Open the Lark group chat where you want notifications
2. Click the group settings (⚙️) → **Bots** → **Add Bot**
3. Search for your app name and add it
4. Get the **Chat ID**: 
   - In the group, type `/chatid` or check the group settings URL

### Step 3: Get Your Sheet Token(s)

From your sheet URL:
```
https://ojpglhhzxlvc.jp.larksuite.com/sheets/OJlkscQ9AhrmWZtTAmEjw8japgV
                                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                               This is your sheet token
```

If you have multiple sheets, separate them with commas.

### Step 4: Sign Up for Carrier APIs (All Free)

| Carrier | Sign Up URL | What You Get |
|---------|-------------|--------------|
| **FedEx** | https://developer.fedex.com | API Key + Secret Key |
| **UPS** | https://developer.ups.com | Client ID + Client Secret |
| **USPS** | https://developer.usps.com | Client ID + Client Secret |
| **DHL** | https://developer.dhl.com | API Key |

Each takes ~5 minutes to register. All free, no credit card needed.

### Step 5: Deploy to GitHub

1. Create a **private** GitHub repo
2. Upload all the bot files to it
3. Go to **Settings → Secrets and variables → Actions**
4. Add these secrets:

| Secret Name | Value |
|-------------|-------|
| `LARK_APP_ID` | Your Lark app ID |
| `LARK_APP_SECRET` | Your Lark app secret |
| `LARK_BASE_URL` | `https://open.larksuite.com` (or JP: `https://open.jp.larksuite.com`) |
| `LARK_CHAT_ID` | Your group chat ID |
| `LARK_SHEET_TOKENS` | Sheet token(s), comma-separated |
| `FEDEX_API_KEY` | FedEx API key |
| `FEDEX_SECRET_KEY` | FedEx secret key |
| `UPS_CLIENT_ID` | UPS client ID |
| `UPS_CLIENT_SECRET` | UPS client secret |
| `USPS_CLIENT_ID` | USPS client ID |
| `USPS_CLIENT_SECRET` | USPS client secret |
| `DHL_API_KEY` | DHL API key |

### Step 6: Test It

1. Go to **Actions** tab in your GitHub repo
2. Click **Shipment Tracking Bot** workflow
3. Click **Run workflow** to trigger manually
4. Check the logs to see it working

## Sheet Format

The bot expects this column layout (matches your existing sheet):

| Col | Field | Bot Reads | Bot Writes |
|-----|-------|-----------|------------|
| A | Shipment ID | ✅ | |
| B | Vendor | ✅ | |
| C | Recipient | ✅ | |
| D | Order # | ✅ | |
| E | Customer | ✅ | |
| F | Product Photo | | |
| G | **Tracking #** | ✅ | |
| H | **Carrier** | ✅ | |
| I | Qty Shipped | | |
| J | Qty Expected | | |
| K | Discrepancy | | |
| L | Balance Owed | | |
| M | **Status** | ✅ | ✅ |
| N | Tariff Charge | | |
| O | # of Boxes | | |
| P | Notes | | |
| Q | **Delivery Date** | | ✅ (new) |

**Add a "Delivery Date" header in column Q, row 2** on each tab.

## Lark Group Chat Message

The bot sends a card message like:

```
📦 Shipment Tracking Update

Daily Tracking Summary — 47 shipments checked

⚠️ EXCEPTION (1)
  • ...47839201 | FedEx | BRENDAN → Wesley Morales | 📍 Memphis, TN

🚚 IN TRANSIT (3)
  • ...41238765 | UPS | CUSTOMER DIRECT → Dominic Vassar
  • ...98234501 | FedEx | BRENDAN → Jaden Mitchell
  • ...11029384 | DHL | BRENDAN → Dakota Cates

✅ DELIVERED (43)
  • ...44476970 | UPS | CUSTOMER DIRECT → Dominic Vassar | 📅 2026-02-20
  • ...84847296 | FedEx | BRENDAN → Berber Visser | 📅 2026-02-18
  ...and 41 more
```

## Adding More Sheets

Just add more sheet tokens to your `LARK_SHEET_TOKENS` secret:

```
OJlkscQ9AhrmWZtTAmEjw8japgV,SecondSheetToken123,ThirdSheetToken456
```

The bot scans all tabs in each spreadsheet (except "TEMPLATE").

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Lark auth failed" | Check LARK_APP_ID and LARK_APP_SECRET |
| "Failed to read spreadsheet" | Make sure the app has `sheets:spreadsheet` permission and the sheet is shared with the app |
| "Unknown carrier" | The bot recognizes: UPS, FedEx, USPS, DHL. Check spelling in column H |
| "FedEx/UPS/USPS/DHL credentials not configured" | Add the carrier API secrets to GitHub |
| Rate limit errors | The bot has a 0.5s delay between API calls. Increase if needed in main.py |

## Running Locally (for testing)

```bash
# Set environment variables
export LARK_APP_ID="your_app_id"
export LARK_APP_SECRET="your_app_secret"
export LARK_BASE_URL="https://open.larksuite.com"
export LARK_CHAT_ID="your_chat_id"
export LARK_SHEET_TOKENS="your_sheet_token"
export FEDEX_API_KEY="..."
# ... set all carrier keys ...

# Install & run
pip install -r requirements.txt
python main.py           # Full run
python main.py --dry-run # Test without writing
```
