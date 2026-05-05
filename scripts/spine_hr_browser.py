"""
Selenium-based SpineHR claims fetcher tuned to the
"Expense > Approve Voucher" page used by Rite Water Solutions.

Page layout (admin view):
  +-------------------------------------------------------------+
  |  IN PROCESS (n) | APPROVED | REJECTED | LAPSED | ALL        |
  +-----+-----+--------------+--------------+----------+--------+
  | [ ] | Edt | Voucher No.  | Emp Code     | Emp Name | ...    |
  |     |     |  2476        | RWSIPL443    | Kamlesh..| ...    |
  |     |     |  Download All|              |          |        |
  +-----+-----+--------------+--------------+----------+--------+

Per-claim downloads (two files, stored together):
  in_process_claims/{EMP_CODE}_{VOUCHER_NO}/
      proofs_{VOUCHER_NO}.zip    <- "Download All" ZIP (attachments)
      voucher_{VOUCHER_NO}.pdf   <- Spine HR system-generated voucher form PDF

Rules:
  - If "File Not Found" when clicking Download All → skip this claim entirely
    (no ZIP, no voucher PDF).
  - If proofs ZIP downloaded OK → also attempt to download the voucher PDF.
  - Only claims NOT already in history/processed_vouchers.json are processed.

Run with --inspect to dump the live DOM so you can update SELECTORS below.

Prereqs:
    pip install selenium webdriver-manager
"""
import os, sys, json, time, re, shutil, argparse, datetime
from pathlib import Path

SKILL_ROOT        = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IN_PROCESS_DIR    = SKILL_ROOT / 'in_process_claims'
DOWNLOAD_STAGING  = IN_PROCESS_DIR / '_browser_downloads'

# ---------------------------------------------------------------------------
# Selectors  (CSS or XPath)
# Run:  python scripts/spine_hr_browser.py --inspect
# to dump the live page DOM and update anything that breaks.
# ---------------------------------------------------------------------------
SELECTORS = {
    # Login form
    'username_field': 'input#txtUser, input[name="txtUser"]',
    'password_field': 'input#txtPassword, input[name="txtPassword"]',
    'login_button':   'input#btnLogin, input[name="btnLogin"]',

    # Side menu
    'claims_menu_xpath':     '//a[normalize-space()="Claims"] | //li[normalize-space()="Claims"]',
    'approve_voucher_xpath': ('//a[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                              '"abcdefghijklmnopqrstuvwxyz"), "approve voucher")] | '
                              '//a[contains(@href, "ApproveVoucher") or contains(@href, "approveVoucher")]'),

    # IN PROCESS tab
    'in_process_tab_xpath': ('//*[self::a or self::button or self::div or self::span]'
                             '[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                             '"abcdefghijklmnopqrstuvwxyz"), "in process")]'),

    # Table rows that have a "Download All" link
    'rows_xpath': '//table//tr[.//a[contains(., "Download All")]]',

    # "Download All" link within a row
    'download_all_xpath': './/a[contains(., "Download All")]',

    # -----------------------------------------------------------------------
    # Spine HR voucher form PDF link — the link in the voucher-number cell
    # that is NOT the "Download All" link.  In most SpineHR versions this is
    # the voucher number itself rendered as a hyperlink.
    #
    # If this selector stops matching after a SpineHR update, run:
    #   python scripts/spine_hr_browser.py --inspect
    # and look for the <a> tag inside td[3] (the Voucher No. cell) whose text
    # is the voucher number (e.g. "2476") or whose href contains "Voucher".
    # -----------------------------------------------------------------------
    'voucher_pdf_in_row_xpath': (
        './/td[3]//a[not(contains(translate(normalize-space(.), '
        '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "download"))]'
        ' | '
        './/td[3]//a[contains(@href,"Voucher") or contains(@href,"voucher")]'
    ),

    # On the voucher view page (if the link navigates there), look for a
    # Print / Download PDF button.
    'voucher_print_xpath': (
        '//*[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        '"abcdefghijklmnopqrstuvwxyz"), "print") or '
        'contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        '"abcdefghijklmnopqrstuvwxyz"), "download pdf") or '
        'contains(@title, "Print") or contains(@title, "PDF")]'
    ),
}


def _load_shared_env():
    shared_env = SKILL_ROOT.parent / '.env.shared'
    if shared_env.exists():
        for line in shared_env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k not in os.environ:
                    os.environ[k] = v


def _safe_name(s):
    return re.sub(r'[^A-Za-z0-9]+', '_', s or '').strip('_').upper()


def _wait_for_download(staging_dir, timeout_s=30, snapshot=None):
    """Wait for a new finished file to appear in staging_dir; return its path.

    Pass snapshot=set(existing_files) to detect only files added after that
    snapshot was taken (avoids re-picking files from a previous download).
    """
    start = time.time()
    if snapshot is None:
        snapshot = set(os.listdir(staging_dir)) if os.path.isdir(staging_dir) else set()
    while time.time() - start < timeout_s:
        time.sleep(0.5)
        if not os.path.isdir(staging_dir):
            continue
        current  = set(os.listdir(staging_dir))
        new_files = current - snapshot
        finished  = [f for f in new_files
                     if not f.endswith('.crdownload') and not f.endswith('.tmp')]
        if finished:
            paths = [os.path.join(staging_dir, f) for f in finished]
            paths.sort(key=os.path.getmtime, reverse=True)
            return paths[0]
    return None


def _dismiss_modal(driver):
    """Try common close-button patterns; fall back to Escape."""
    from selenium.webdriver.common.by import By
    dismissed = False
    for xpath in [
        '//*[contains(@class,"close")]',
        '//*[@aria-label="Close" or @data-dismiss="modal"]',
        '//*[text()="×" or text()="✕"]',
        '//button[contains(@class,"btn")]',
    ]:
        try:
            driver.find_element(By.XPATH, xpath).click()
            dismissed = True
            break
        except Exception:
            pass
    if not dismissed:
        from selenium.webdriver.common.keys import Keys
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
    time.sleep(1)


def fetch_in_process_claims(headless=False, inspect=False, dry_run=False, max_claims=50):
    _load_shared_env()
    url  = os.environ.get('SPINEHR_URL')
    user = os.environ.get('SPINEHR_USERNAME')
    pw   = os.environ.get('SPINEHR_PASSWORD')
    if not url or not user or not pw:
        print('ERROR: SPINEHR_URL / SPINEHR_USERNAME / SPINEHR_PASSWORD not set '
              'in .env.shared or environment', file=sys.stderr)
        sys.exit(2)

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as e:
        print(f'ERROR: selenium/webdriver-manager missing.\n  pip install selenium webdriver-manager\n  {e}',
              file=sys.stderr)
        sys.exit(3)

    DOWNLOAD_STAGING.mkdir(parents=True, exist_ok=True)
    options = Options()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--window-size=1600,1000')
    options.add_experimental_option('prefs', {
        'download.default_directory':   str(DOWNLOAD_STAGING),
        'download.prompt_for_download': False,
        'download.directory_upgrade':   True,
        'plugins.always_open_pdf_externally': True,
    })

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                               options=options)
    wait      = WebDriverWait(driver, 25)
    new_claims = []

    try:
        print(f'[browser] opening {url}')
        driver.get(url)

        # ---- Login ----
        print('[browser] logging in...')
        try:
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, SELECTORS['username_field'])))

            # Dismiss cookie-consent modal FIRST
            try:
                accept_btn = driver.find_element(
                    By.CSS_SELECTOR, 'input#btnAccept, input[name="btnAccept"]')
                driver.execute_script("arguments[0].click();", accept_btn)
                time.sleep(1)
            except Exception:
                pass

            # Set field values via JavaScript (avoids ASP.NET WebForms race issues)
            driver.execute_script("""
                var u = document.getElementById('txtUser');
                var p = document.getElementById('txtPassword');
                u.value = arguments[0];
                p.value = arguments[1];
                u.dispatchEvent(new Event('input',  {bubbles: true}));
                u.dispatchEvent(new Event('change', {bubbles: true}));
                p.dispatchEvent(new Event('input',  {bubbles: true}));
                p.dispatchEvent(new Event('change', {bubbles: true}));
            """, user, pw)

            login_btn = driver.find_element(By.CSS_SELECTOR, SELECTORS['login_button'])
            driver.execute_script("arguments[0].click();", login_btn)

            try:
                wait.until(EC.url_changes(url))
            except Exception:
                time.sleep(5)
        except Exception as e:
            print(f'[browser] login form not found ({e}); '
                  'continuing — session may already be active.', file=sys.stderr)

        if inspect:
            out = SKILL_ROOT / 'history' / f'spinehr_dump_{datetime.date.today().isoformat()}.html'
            out.write_text(driver.page_source, encoding='utf-8')
            print(f'--- PAGE TITLE ---\n{driver.title}')
            print(f'--- CURRENT URL ---\n{driver.current_url}')
            print(f'--- DOM written to {out} ---')
            print('First 4000 chars of page source:')
            print(driver.page_source[:4000])
            return []

        # ---- Navigate: Claims -> Approve Voucher ----
        print('[browser] navigating to Claims -> Approve Voucher ...')
        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, SELECTORS['claims_menu_xpath']))).click()
            time.sleep(1)
            wait.until(EC.presence_of_element_located(
                (By.XPATH, SELECTORS['approve_voucher_xpath']))).click()
            time.sleep(2)
        except Exception as e:
            print(f'[browser] menu navigation failed ({e}).', file=sys.stderr)

        # ---- Click IN PROCESS tab ----
        try:
            tab = wait.until(EC.element_to_be_clickable(
                (By.XPATH, SELECTORS['in_process_tab_xpath'])))
            tab.click()
            time.sleep(2)
        except Exception as e:
            print(f'[browser] could not click IN PROCESS tab ({e}); '
                  'proceeding with current view.', file=sys.stderr)

        # ---- Dedup state ----
        sys.path.insert(0, str(SKILL_ROOT / 'scripts'))
        import state_store
        seen_voucher_keys = {
            f"{p['employee_code']}__{p['voucher_no']}"
            for p in state_store.list_processed()
        }

        # ---- Snapshot all row data BEFORE any downloads ----
        rows = driver.find_elements(By.XPATH, SELECTORS['rows_xpath'])
        print(f'[browser] found {len(rows)} in-process claim row(s)')
        claims_snapshot = []
        for i, row in enumerate(rows[:max_claims]):
            try:
                tds        = row.find_elements(By.TAG_NAME, 'td')
                voucher_cell = tds[2].text if len(tds) > 2 else ''
                voucher_no   = re.split(r'\s|\n', voucher_cell.strip())[0] if voucher_cell else ''
                emp_code     = tds[3].text.strip() if len(tds) > 3 else ''
                emp_name     = tds[4].text.strip() if len(tds) > 4 else ''
                if not re.fullmatch(r'\d+', voucher_no):
                    continue
                claims_snapshot.append((voucher_no, emp_code, emp_name))
            except Exception as e:
                print(f'  row {i}: could not read ({e})')

        # ---- Process each claim ----
        for idx, (voucher_no, emp_code, emp_name) in enumerate(claims_snapshot):
            key = f'{emp_code}__{voucher_no}'
            if any(key in s for s in seen_voucher_keys):
                print(f'  skip: voucher #{voucher_no} ({emp_name} / {emp_code}) already audited')
                continue

            # Folder named {EMP_CODE}_{VOUCHER_NO} — easy to parse in batch_audit
            target = IN_PROCESS_DIR / f'{emp_code}_{voucher_no}'
            target.mkdir(parents=True, exist_ok=True)
            print(f'  {idx:02d}: voucher #{voucher_no} | {emp_code} | {emp_name} -> {target.name}')
            if dry_run:
                continue

            zip_dest     = target / f'proofs_{voucher_no}.zip'
            pdf_dest     = target / f'voucher_{voucher_no}.pdf'

            # ------------------------------------------------------------------
            # Check disk: move any legacy ZIP from _browser_downloads/ first.
            # Legacy filename pattern: {EMP_CODE}_{VOUCHER_NO}_{TIMESTAMP}.zip
            # ------------------------------------------------------------------
            if not zip_dest.exists():
                for legacy in DOWNLOAD_STAGING.glob(f'{emp_code}_{voucher_no}_*.zip'):
                    shutil.move(str(legacy), zip_dest)
                    print(f'    moved existing ZIP: {zip_dest.name}')
                    break

            zip_exists = zip_dest.exists()
            pdf_exists = pdf_dest.exists()

            if zip_exists and pdf_exists:
                print(f'    both files already present — skipping')
                new_claims.append({
                    'voucher_no': voucher_no, 'employee_code': emp_code,
                    'employee_name': emp_name, 'folder': str(target),
                    'proofs_zip': str(zip_dest), 'voucher_pdf': str(pdf_dest),
                })
                continue

            # ------------------------------------------------------------------
            # STEP 1: Download proofs ZIP ("Download All") — skip if already have it
            # ------------------------------------------------------------------
            if zip_exists:
                print(f'    proofs ZIP already present — skipping ZIP download')
            else:
                staging_snap1 = set(os.listdir(str(DOWNLOAD_STAGING)))
                dl_link = None
                try:
                    live_rows = driver.find_elements(By.XPATH, SELECTORS['rows_xpath'])
                    for lr in live_rows:
                        try:
                            tds         = lr.find_elements(By.TAG_NAME, 'td')
                            cell_text   = tds[2].text.strip() if len(tds) > 2 else ''
                            row_voucher = re.split(r'\s|\n', cell_text)[0]
                            if row_voucher == voucher_no:
                                dl_link = lr.find_element(
                                    By.XPATH, SELECTORS['download_all_xpath'])
                                driver.execute_script(
                                    "arguments[0].scrollIntoView({block:'center'});", dl_link)
                                time.sleep(0.3)
                                dl_link.click()
                                break
                        except Exception:
                            continue
                except Exception as e:
                    print(f'    ZIP download click failed: {e}')
                    continue

                if not dl_link:
                    print(f'    could not find row for voucher #{voucher_no} — skipping')
                    continue

                # Check for "File Not Found" modal (no attachments uploaded)
                time.sleep(2)
                file_not_found = False
                try:
                    err_el = driver.find_element(
                        By.XPATH,
                        '//*[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                        '"abcdefghijklmnopqrstuvwxyz"), "file not found")]'
                    )
                    if err_el.is_displayed() and err_el.text.strip():
                        file_not_found = True
                except Exception:
                    pass

                if file_not_found:
                    print(f'    skip: "File Not Found" — no proofs for #{voucher_no}')
                    _dismiss_modal(driver)
                    try:
                        target.rmdir()
                    except Exception:
                        pass
                    continue  # no ZIP → skip voucher PDF too

                downloaded_zip = _wait_for_download(str(DOWNLOAD_STAGING),
                                                    timeout_s=45, snapshot=staging_snap1)
                if not downloaded_zip:
                    print(f'    timed out waiting for proofs ZIP')
                    continue

                shutil.move(downloaded_zip, zip_dest)
                print(f'    proofs ZIP saved: {zip_dest.name}')
                zip_exists = True

            # ------------------------------------------------------------------
            # STEP 2: Download Spine HR voucher form PDF — skip if already have it
            # ------------------------------------------------------------------
            if pdf_exists:
                print(f'    voucher PDF already present — skipping PDF download')
                voucher_pdf_dest = pdf_dest
            else:
                staging_snap2 = set(os.listdir(str(DOWNLOAD_STAGING)))
                main_window   = driver.current_window_handle
                voucher_pdf_dest = None
                try:
                    live_rows2 = driver.find_elements(By.XPATH, SELECTORS['rows_xpath'])
                    vlink = None
                    for lr2 in live_rows2:
                        try:
                            tds2 = lr2.find_elements(By.TAG_NAME, 'td')
                            ct2  = tds2[2].text.strip() if len(tds2) > 2 else ''
                            if re.split(r'\s|\n', ct2)[0] == voucher_no:
                                vlink = lr2.find_element(
                                    By.XPATH, SELECTORS['voucher_pdf_in_row_xpath'])
                                break
                        except Exception:
                            continue

                    if vlink:
                        href = vlink.get_attribute('href') or ''
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", vlink)
                        time.sleep(0.2)

                        if href and not href.lower().startswith('javascript'):
                            # Open in new tab — keeps the claims list intact
                            driver.execute_script(
                                "window.open(arguments[0], '_blank');", href)
                            time.sleep(1)
                            new_tab = [w for w in driver.window_handles if w != main_window]
                            if new_tab:
                                driver.switch_to.window(new_tab[-1])
                                time.sleep(2)
                                try:
                                    print_btn = driver.find_element(
                                        By.XPATH, SELECTORS['voucher_print_xpath'])
                                    driver.execute_script(
                                        "arguments[0].click();", print_btn)
                                    time.sleep(1)
                                except Exception:
                                    pass
                                vpdf_path = _wait_for_download(
                                    str(DOWNLOAD_STAGING), timeout_s=30,
                                    snapshot=staging_snap2)
                                driver.close()
                                driver.switch_to.window(main_window)
                            else:
                                vpdf_path = None
                        else:
                            # JavaScript link — click in current tab
                            driver.execute_script("arguments[0].click();", vlink)
                            vpdf_path = _wait_for_download(
                                str(DOWNLOAD_STAGING), timeout_s=30,
                                snapshot=staging_snap2)
                            if driver.current_url != url:
                                driver.back()
                                time.sleep(2)

                        if vpdf_path:
                            vext = os.path.splitext(vpdf_path)[1].lower() or '.pdf'
                            voucher_pdf_dest = target / f'voucher_{voucher_no}{vext}'
                            shutil.move(vpdf_path, voucher_pdf_dest)
                            print(f'    voucher PDF saved: {voucher_pdf_dest.name}')
                        else:
                            print(f'    voucher PDF: download timed out')
                    else:
                        print(f'    voucher PDF: no link found in row '
                              f'(run --inspect to update SELECTORS["voucher_pdf_in_row_xpath"])')

                except Exception as e:
                    print(f'    voucher PDF error: {e}')
                    try:
                        if driver.current_window_handle != main_window:
                            driver.close()
                            driver.switch_to.window(main_window)
                    except Exception:
                        pass

            # Record result
            new_claims.append({
                'voucher_no':    voucher_no,
                'employee_code': emp_code,
                'employee_name': emp_name,
                'folder':        str(target),
                'proofs_zip':    str(zip_dest),
                'voucher_pdf':   str(voucher_pdf_dest) if voucher_pdf_dest else None,
            })

        return new_claims
    finally:
        driver.quit()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--inspect', action='store_true',
                    help='Login then dump the page DOM to history/spinehr_dump_YYYY-MM-DD.html')
    ap.add_argument('--dry-run', action='store_true',
                    help='List claims without downloading')
    ap.add_argument('--max', type=int, default=50)
    args = ap.parse_args()
    out = fetch_in_process_claims(headless=False, inspect=args.inspect,
                                   dry_run=args.dry_run, max_claims=args.max)
    print(json.dumps(out, indent=2))
