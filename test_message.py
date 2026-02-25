"""
Hypothetical test: sends a fake daily summary to Lark
with sample FedEx, UPS, and DHL shipments.
Delete this file after testing.
"""
import logging
from lark_client import LarkClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

fake_results = [
    {
        "tracking_num": "794644790138",
        "carrier": "FEDEX",
        "recipient": "Brendan",
        "customer": "",
        "new_status": "IN TRANSIT",
        "delivery_date": "2026-02-27",
        "raw_status": "In transit",
    },
    {
        "tracking_num": "794644790999",
        "carrier": "FEDEX",
        "recipient": "Customer Direct",
        "customer": "James Wilson",
        "new_status": "OUT FOR DELIVERY",
        "delivery_date": "2026-02-25",
        "raw_status": "Out for delivery",
    },
    {
        "tracking_num": "1Z999AA10123456784",
        "carrier": "UPS",
        "recipient": "Brendan",
        "customer": "",
        "new_status": "IN TRANSIT",
        "delivery_date": "2026-02-28",
        "raw_status": "In Transit",
    },
    {
        "tracking_num": "1Z999AA10987654321",
        "carrier": "UPS",
        "recipient": "Customer Direct",
        "customer": "Sarah Chen",
        "new_status": "LABEL CREATED",
        "delivery_date": "",
        "raw_status": "Label Created",
    },
    {
        "tracking_num": "1234567890",
        "carrier": "DHL",
        "recipient": "Brendan",
        "customer": "",
        "new_status": "IN TRANSIT",
        "delivery_date": "2026-03-03",
        "raw_status": "In transit",
    },
    {
        "tracking_num": "9876543210",
        "carrier": "DHL",
        "recipient": "Customer Direct",
        "customer": "Mike Rodriguez",
        "new_status": "IN TRANSIT",
        "delivery_date": "2026-03-01",
        "raw_status": "Shipment picked up",
    },
]

if __name__ == "__main__":
    lark = LarkClient()
    logger.info("Sending HYPOTHETICAL test message with fake FedEx/UPS/DHL data...")
    lark.send_daily_summary(fake_results)
    logger.info("Test message sent!")
