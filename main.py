import os
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, render_template, jsonify
import gspread  # pip install gspread google-auth
from oauth2client.service_account import ServiceAccountCredentials
from threading import Thread

# Google Sheets Setup
SCOPE = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
GSHEET_ID = os.environ.get("GSHEET_ID")
GSHEET_CRED_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "gcreds.json")

def get_gsheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(GSHEET_CRED_FILE, SCOPE)
    client = gspread.authorize(creds)
    return client.open_by_key(GSHEET_ID).sheet1

# MAIN SCRAPER LOGIC
def nhs_scrape(location="London", pay_bands=None, contract_types=None, **kwargs):
    jobs = []
    session = requests.Session()
    base_url = "https://www.jobs.nhs.uk/candidate/search/results"
    page = 1
    max_pages = kwargs.get('max_pages', 50)
    params = {
        "location": location,
        "sort": "publicationDateDesc",
        "language": "en",
        "page": page
    }
    if pay_bands:
        params["payBand"] = ",".join(pay_bands)
    if contract_types:
        params["contractType"] = ",".join(contract_types)
    
    last_page = 1
    while page <= max_pages:
        params['page'] = page
        print(f"Fetching page {page}...")
        r = session.get(base_url, params=params, timeout=15)
        if not r.ok:
            print("Error fetching page", page)
            break
        soup = BeautifulSoup(r.text, "html.parser")
        job_cards = soup.find_all("li", {"data-test": "search-result"})
        if not job_cards:
            break
        # Parse jobs
        for jc in job_cards:
            title_elem = jc.find("a", {"data-test": "search-result-job-title"})
            salary_elem = jc.find("li", {"data-test": "search-result-salary"})
            job_type_elem = jc.find("li", {"data-test": "search-result-jobType"})
            pub_date_elem = jc.find("li", {"data-test": "search-result-publicationDate"})
            closing_date_elem = jc.find("li", {"data-test": "search-result-closingDate"})
            loc_elem = jc.find("div", {"data-test": "search-result-location"})
            jobs.append({
                "title": title_elem.get_text(strip=True) if title_elem else None,
                "link": "https://www.jobs.nhs.uk" + title_elem['href'] if title_elem else None,
                "salary": salary_elem.get_text(strip=True) if salary_elem else None,
                "job_type": job_type_elem.get_text(strip=True) if job_type_elem else None,
                "publication_date": pub_date_elem.get_text(strip=True) if pub_date_elem else None,
                "closing_date": closing_date_elem.get_text(strip=True) if closing_date_elem else None,
                "location": loc_elem.get_text(strip=True) if loc_elem else None
            })
        # Pagination parsing (Page X of Y)
        pag_text = soup.find("span", {"class": "nhsuk-pagination__page"})
        if pag_text:
            import re
            m = re.search(r"Page \d+ of (\d+)", pag_text.text)
            if m:
                last_page = int(m.group(1))
        if page >= last_page:
            break
        # RATE LIMIT
        time.sleep(60)  # 1 request per minute
        page += 1
    return jobs

def save_to_gsheet(jobs):
    sh = get_gsheet()
    sh.clear()
    sh.append_row(["title", "link", "salary", "job_type", "publication_date", "closing_date", "location"])
    for job in jobs:
        sh.append_row([job.get("title"), job.get("link"), job.get("salary"),
                       job.get("job_type"), job.get("publication_date"),
                       job.get("closing_date"), job.get("location")])

# Flask web app
app = Flask(__name__)

@app.route("/")
def frontend():
    return render_template("index.html")

@app.route("/api/scrape", methods=["GET"])
def trigger_scrape():
    location = request.args.get("location", "London")
    pay_bands = request.args.getlist("payBand")
    contract_types = request.args.getlist("contractType")
    jobs = nhs_scrape(location=location, pay_bands=pay_bands, contract_types=contract_types, max_pages=10)
    save_to_gsheet(jobs)
    return jsonify({"jobs": jobs, "total": len(jobs)})

@app.route("/api/jobs", methods=["GET"])
def api_get_jobs():
    sh = get_gsheet()
    jobs = sh.get_all_records()
    return jsonify(jobs)

# Run scheduled scrape every 5 min (threaded, for demo - use scheduler in production)
def background_scraper():
    while True:
        print("Scheduled scrape...")
        try:
            jobs = nhs_scrape(max_pages=10)
            save_to_gsheet(jobs)
        except Exception as e:
            print("Error in scheduled scrape:", e)
        time.sleep(300)  # every 5 min

if os.environ.get("FLASK_RUN_FROM_CLI", "0") == "1":
    Thread(target=background_scraper, daemon=True).start()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
