def welcome_message():
    return (
        "👋 Welcome!\n\n"
        "Please choose an option:\n"
        "1️⃣ Insurance\n"
        "2️⃣ Sales\n\n"
        "Reply with 1 or 2"
    )

def main_menu_buttons():
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": "👋 Welcome!\nPlease choose an option:"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "insurance",
                            "title": "🔰 Insurance"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "sales",
                            "title": "💼 Sales"
                        }
                    }
                ]
            }
        }
    }

def sales_welcome_message():
    return (
        "Hi there! 👋 Welcome to *AutoSherpa* 🚗\n\n"
        "Searching for a new Hyundai car in Bengaluru?\n"
        "I can help you with prices, brochure and book a test drive instantly.\n\n"
        "*Main Menu*\n"
        "Please select an option:"
    )


def invalid_entry_message(user_text: str):
    return (
        f"👋 Sorry, I can’t assist with the request *'{user_text}'*.\n\n"
        "I’m here to help with car service bookings, estimates, and vehicle-related information.\n\n"
        "If you need support for something else, I can connect you with a live service advisor.\n\n"
        "Type *Hi* to start the service flow to connect with a human agent."
    )
