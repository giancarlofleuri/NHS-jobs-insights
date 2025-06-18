import os
# If deployed on Render, decode the credentials from environment variable
if os.environ.get("GOOGLE_CREDS_JSON"):
    with open("gcreds.json", "w") as f:
        f.write(os.environ["GOOGLE_CREDS_JSON"])
import time
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from threading import Thread

# Google Sheets setup
SCOPE = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
GSHEET_ID = os.getenv("GSHEET_ID")  # Set this in your Replit/Render env
GSHEET_CRED_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "gcreds.json")

def get_gsheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(GSHEET_CRED_FILE, SCOPE)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GSHEET_ID).worksheet("Sheet1")

def parse_salary(s):
    nums = [int(x.replace(',', '')) for x in re.findall(r"\d{2,3}[,]?\d{3}", s)]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums), max(nums)

def extract_band(title):
    m = re.search(r'band[\s\-]*(\d+)', title.lower())
    return f"BAND_{m.group(1)}" if m else ""

def scrape_nhs_jobs(location="London", pay_bands=None, max_pages=20, delay=2):
    base = "https://www.jobs.nhs.uk/candidate/search/results"
    jobs = []
    params = {
        "location": location,
        "sort": "publicationDateDesc",
        "language": "en",
        "page": 1
    }
    if pay_bands:
        params["payBand"] = ",".join(pay_bands)
    seen_ids = set()
    for p in range(1, max_pages+1):
        params["page"] = p
        try:
            resp = requests.get(base, params=params, timeout=30)
            if resp.status_code != 200:
                break
        except Exception:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        listings = soup.find_all("li", {"data-test": "search-result"})
        if not listings:
            break
        for item in listings:
            title_elem = item.find("a", {"data-test": "search-result-job-title"})
            job_id = None
            app_url = title_elem['href'] if title_elem and title_elem.has_attr("href") else ""
            if app_url:
                m = re.search(r'/jobadvert/([^/?]+)', app_url)
                job_id = m.group(1) if m else ""
            else:
                continue
            if job_id in seen_ids:  # avoid dups across pagination
                continue
            seen_ids.add(job_id)
            salary = item.find("li", {"data-test": "search-result-salary"})
            sal_txt = salary.text.strip() if salary else ""
            sal_min, sal_max = parse_salary(sal_txt)
            loc = item.find("div", {"data-test": "search-result-location"})
            post_date = item.find("li", {"data-test": "search-result-publicationDate"})
            jobs.append({
                "job_id": job_id,
                "title": title_elem.get_text(strip=True) if title_elem else "",
                "location": loc.text.strip() if loc else "",
                "salary_text": sal_txt,
                "salary_min": sal_min,
                "salary_max": sal_max,
                "application_url": "https://www.jobs.nhs.uk" + app_url if app_url.startswith("/") else app_url,
                "band": extract_band(title_elem.get_text(strip=True)) if title_elem else "",
                "posting_date": post_date.text.strip() if post_date else ""
            })
        time.sleep(delay)
    return jobs

def dedupe_sync(jobs):
    ws = get_gsheet()
    records = ws.get_all_records()
    existing = {str(row["job_id"]): row for row in records}
    jobs_by_id = {str(job["job_id"]): job for job in jobs}
    # new & updated
    new, updated = [], []
    for job_id, job in jobs_by_id.items():
        if job_id not in existing:
            job["status"] = "new"
            new.append(job)
        else:
            ex = existing[job_id]
            if (str(job["salary_min"]) != str(ex.get("salary_min")) or 
                str(job["salary_max"]) != str(ex.get("salary_max")) or 
                job["location"] != ex.get("location")):
                job["status"] = "updated"
                updated.append(job)
    # closed
    closed = []
    for ex_id, ex_job in existing.items():
        if ex_id not in jobs_by_id and ex_job.get("status", "").lower() != "closed":
            ex_job["status"] = "closed"
            ex_job["closed_at"] = time.strftime('%Y-%m-%d')
            closed.append(ex_job)
    # Write new/updates/closed back
    # Clear and write all, simple and robust
    fields = [
        "job_id", "title", "location", "salary_text", "salary_min", "salary_max", "application_url",
        "band", "posting_date", "status", "closed_at"
    ]
    all_now = list(jobs_by_id.values()) + closed
    ws.clear()
    ws.append_row(fields)
    for job in all_now:
        ws.append_row([job.get(f, "") for f in fields])
    return dict(new=len(new), updated=len(updated), closed=len(closed))

# Flask app
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/jobs')
def api_jobs():
    ws = get_gsheet()
    rows = ws.get_all_records()
    # Optionally filter by status or search
    status = request.args.get("status")
    q = request.args.get("q", "").strip().lower()
    filtered = []
    for r in rows:
        if status and r.get("status", "").lower() != status.lower():
            continue
        if q and not any(q in str(r.get(f, "")).lower() for f in ["title", "location", "band"]):
            continue
        filtered.append(r)
    return jsonify(filtered)

@app.route('/api/scrape', methods=["POST"])
def do_scrape():
    data = request.get_json() or {}
    location = data.get("location", "London")
    bands = data.get("bands", ["BAND_4", "BAND_5"])
    max_pages = int(data.get("max_pages", 5))
    delay = int(data.get("delay", 2))
    jobs = scrape_nhs_jobs(location, bands, max_pages, delay)
    stats = dedupe_sync(jobs)
    return jsonify({"stats": stats})

# --- Optional: background job for scheduled scraping ----
def scraper_job():
    while True:
        try:
            print("Scheduled scrape...")
            jobs = scrape_nhs_jobs(max_pages=10)
            dedupe_sync(jobs)
        except Exception as e:
            print("Scrape error:", e)
        time.sleep(300)  # every 5 minutes

if os.environ.get("SCHEDULE") == "1":
    Thread(target=scraper_job, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=True)