"""
main.py
-------
Entry point for the bot. Run this file to start scraping.

Usage (interactive):
    python main.py

Usage (command-line arguments):
    python main.py --keyword "Civil" --city "Jaipur"

Data flow (see README for details):
    Google Maps -> Website URL -> Website Scraping -> Contact Pages
    -> Email Extraction -> Clean Data -> Final Output (.xlsx / .csv)
"""

import argparse
import time

import config
from gmaps_scraper import scrape_google_maps
from email_extractor import find_contacts, classify_social_link
from utils import remove_duplicates, save_results


def run_bot(keyword, city):
    """
    Orchestrates the full pipeline: scrape listings, enrich each one with
    contact details from its website, clean up duplicates, and save.

    Phone, WhatsApp and Email stay in three separate columns on purpose.
    They come from different places and are often genuinely different
    numbers -- the Maps phone is frequently a reception line or landline,
    while the WhatsApp on a company's own site tends to reach the owner or
    admissions desk directly. Merging them would hide exactly the
    distinction that makes one more useful than the other.
    """
    print("=" * 60)
    print(f"Starting bot for keyword='{keyword}', city='{city}'")
    print("=" * 60)

    # STEP 1: Scrape business listings from Google Maps
    listings = scrape_google_maps(keyword, city)
    print(f"\nCollected {len(listings)} business listings from Google Maps.\n")

    if not listings:
        print("No listings found. Try a different keyword/city.")
        return

    # STEP 2: Visit each company's website for an email and a WhatsApp number
    print("Extracting emails and WhatsApp numbers from company websites...")
    found_email = 0
    found_whatsapp = 0
    found_social = 0
    # Keyed by the dict key find_contacts returns, valued by the exact column
    # name utils.py expects. Not built with .capitalize(), which would turn
    # "linkedin" into "Linkedin" and quietly miss the column order entirely.
    SOCIAL_COLUMNS = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "linkedin": "LinkedIn",
        "youtube": "YouTube",
    }

    for i, business in enumerate(listings, start=1):
        website = business.get("Website", "")
        print(f"  [{i}/{len(listings)}] {business.get('Company Name', 'Unknown')}")

        if website:
            # One pass over the site collects both, so turning WhatsApp on
            # costs no extra requests
            business.pop("_listed_link", None)
            contacts = find_contacts(website)
            business["Email"] = contacts["email"]
            business["WhatsApp"] = contacts["whatsapp"]
            for key, column in SOCIAL_COLUMNS.items():
                business[column] = contacts.get(key, "")

            if contacts["email"]:
                found_email += 1
                print(f"      Found email: {contacts['email']}")
            else:
                print("      No email found.")

            if contacts["whatsapp"]:
                found_whatsapp += 1
                print(f"      Found WhatsApp: {contacts['whatsapp']}")

            platforms = [c for k, c in SOCIAL_COLUMNS.items() if contacts.get(k)]
            if platforms:
                found_social += 1
                print(f"      Found social: {', '.join(platforms)}")
        else:
            business["Email"] = ""
            business["WhatsApp"] = ""
            for column in SOCIAL_COLUMNS.values():
                business[column] = ""

            # Maps sometimes carries a Facebook or Instagram page in place of
            # a website. It is not a website, but it is still a way to reach
            # them, so file it under the right social column.
            listed = business.pop("_listed_link", "")
            if listed:
                platform, profile = classify_social_link(listed)
                if platform:
                    business[platform] = profile
                    found_social += 1
                    print(f"      No website, but found {platform}: {profile}")
                else:
                    print("      No website listed, skipping website lookup.")
            else:
                print("      No website listed, skipping website lookup.")

        # Be polite to the target websites — avoid hammering them with
        # rapid-fire requests
        time.sleep(config.WEBSITE_REQUEST_DELAY)

    with_phone = sum(1 for b in listings if b.get("Phone Number"))
    print(f"\nContact details found across {len(listings)} businesses:")
    print(f"  Phone (from Maps) : {with_phone}")
    print(f"  WhatsApp (website): {found_whatsapp}")
    print(f"  Email (website)   : {found_email}")
    print(f"  Social profiles   : {found_social}")

    # STEP 3: Clean up duplicates
    print("\nCleaning data...")
    listings = remove_duplicates(listings)

    # STEP 4: Save to Excel + CSV
    save_results(listings, keyword, city)

    print("\nDone!")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape business leads (with emails) from Google Maps."
    )
    parser.add_argument("--keyword", help="Industry/keyword to search for, e.g. 'Civil'")
    parser.add_argument("--city", help="City to search in, e.g. 'Jaipur'")
    args = parser.parse_args()

    keyword = args.keyword
    city = args.city

    if not keyword or not city:
        print("Both keyword and city are required.")
        return

    run_bot(keyword, city)


from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running 🚀"

@app.route("/run", methods=["GET"])
def run_api():
    keyword = request.args.get("keyword")
    city = request.args.get("city")

    print("RUN API HIT", keyword, city, flush=True)

    if not keyword or not city:
        return jsonify({"error": "keyword and city required"}), 400

    run_bot(keyword, city)

    return jsonify({
        "status": "success",
        "keyword": keyword,
        "city": city
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
