"""Getting an authenticated `requests` session for Schoology.

The browser is expensive and fragile, so it is the fallback rather than the
rule: cookies from a successful sign-in are stored on disk and reused until
Schoology stops accepting them.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry

from config import Settings, course_id_from_url

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    """Signing in failed."""


class HomeroomNotFound(LoginError):
    """The course link we navigate to in order to learn the course id is gone."""


class SchoologyAuth:
    def __init__(self, settings: Settings, known_course_id: str = "") -> None:
        self._settings = settings
        self.course_id = known_course_id
        self.screenshot: Path | None = None

    def session(self) -> requests.Session:
        """An authenticated session, from cookies if possible."""
        session = self._build_session()

        if self._apply_stored_cookies(session) and self._is_authenticated(session):
            logger.info("Reusing the stored Schoology session")
            return session

        logger.info("No usable session on disk, signing in through the browser")
        cookies = self._browser_login()
        self._store_cookies(cookies)
        session = self._build_session()
        _apply_cookies(session, cookies)

        if not self._is_authenticated(session):
            raise LoginError("Signed in through the browser but the session is still rejected")
        return session

    # --- session plumbing -------------------------------------------------

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _is_authenticated(self, session: requests.Session) -> bool:
        url = f"{self._settings.base_url}/home"
        try:
            response = session.get(url, timeout=self._settings.http_timeout)
        except requests.RequestException as exc:
            logger.warning("Session probe failed: %s", exc)
            return False

        final = response.url
        signed_in = (
            response.ok
            and "schoology.com" in final
            and "/login" not in final
            and "microsoftonline.com" not in final
        )
        logger.info("Session probe -> %s (%s)", final, "valid" if signed_in else "expired")
        return signed_in

    # --- cookie storage ---------------------------------------------------

    def _apply_stored_cookies(self, session: requests.Session) -> bool:
        path = self._settings.cookie_file
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable cookie file %s: %s", path, exc)
            return False

        now = time.time()
        fresh = [c for c in cookies if not c.get("expiry") or c["expiry"] > now]
        if len(fresh) != len(cookies):
            logger.info("Dropped %d expired cookies", len(cookies) - len(fresh))
        if not fresh:
            return False

        logger.info("Loaded %d cookies from %s", len(fresh), path)
        _apply_cookies(session, fresh)
        return True

    def _store_cookies(self, cookies: list[dict]) -> None:
        path = self._settings.cookie_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        tmp.replace(path)
        path.chmod(0o600)
        logger.info("Saved %d cookies to %s", len(cookies), path)

    # --- browser sign-in --------------------------------------------------

    @contextmanager
    def _browser(self) -> Iterator[webdriver.Chrome]:
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-search-engine-choice-screen")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(f"--user-agent={USER_AGENT}")
        if self._settings.headless:
            options.add_argument("--headless=new")

        driver = webdriver.Chrome(options=options)
        try:
            yield driver
        except Exception:
            self.screenshot = self._capture(driver)
            raise
        finally:
            # close() only drops the window; quit() is what reaps chromedriver.
            try:
                driver.quit()
            except WebDriverException as exc:
                logger.warning("Failed to shut the browser down cleanly: %s", exc)

    def _capture(self, driver: webdriver.Chrome) -> Path | None:
        path = self._settings.screenshot_file
        try:
            driver.save_screenshot(str(path))
        except WebDriverException as exc:
            logger.warning("Could not take a screenshot: %s", exc)
            return None
        logger.info("Saved a screenshot of the failure to %s", path)
        return path

    def _browser_login(self) -> list[dict]:
        settings = self._settings
        with self._browser() as driver:
            wait = WebDriverWait(driver, settings.selenium_timeout)

            logger.info("Opening %s", settings.base_url)
            driver.get(settings.base_url)

            logger.info("Entering the email address")
            email_field = wait.until(EC.element_to_be_clickable((By.NAME, "loginfmt")))
            email_field.clear()
            email_field.send_keys(settings.email)
            _click(driver, wait, (By.ID, "idSIButton9"), "Next")

            logger.info("Entering the password")
            password_field = wait.until(EC.element_to_be_clickable((By.NAME, "passwd")))
            password_field.clear()
            password_field.send_keys(settings.password)
            _click(driver, wait, (By.ID, "idSIButton9"), "Sign in")

            # Microsoft shows "Stay signed in?" only sometimes.
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.ID, "KmsiCheckboxField"))
                )
            except TimeoutException:
                logger.info("No 'Stay signed in' prompt")
            else:
                _click(driver, wait, (By.ID, "idSIButton9"), "Stay signed in")

            wait.until(lambda d: "schoology.com" in d.current_url)
            logger.info("Signed in, landed on %s", driver.current_url)

            self._switch_to_child(driver, wait)

            if not self.course_id:
                self.course_id = self._discover_course_id(driver, wait)
            else:
                logger.info("Course id %s already known, skipping the course lookup", self.course_id)

            return driver.get_cookies()

    def _switch_to_child(self, driver: webdriver.Chrome, wait: WebDriverWait) -> None:
        """Parent accounts have to switch into the child before the feed is visible."""
        try:
            menu = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, '//div[contains(text(), "Parents of")]'))
            )
        except TimeoutException:
            logger.info("No parent account switcher on the page, continuing as-is")
            return

        logger.info("Switching to the child account")
        menu.click()
        link = wait.until(
            EC.element_to_be_clickable((By.XPATH, '//a[contains(@href,"/parent/switch_child/")]'))
        )
        link.click()
        wait.until(EC.staleness_of(link))

    def _discover_course_id(self, driver: webdriver.Chrome, wait: WebDriverWait) -> str:
        name = self._settings.homeroom_course_name
        logger.info("Looking for the %r course to learn its id", name)
        try:
            link = wait.until(
                EC.element_to_be_clickable((By.XPATH, f'//a[contains(text(),"{name}")]'))
            )
        except TimeoutException:
            visible = _visible_courses(driver)
            raise HomeroomNotFound(
                f"No course link matching {name!r} was found. "
                f"Courses currently visible: {visible or 'none'}. "
                "Set SCHOOLOGY_COURSE_ID (or HOMEROOM_COURSE_URL) so the run stops "
                "depending on this lookup, or set HOMEROOM_COURSE_NAME if the course "
                "was renamed."
            ) from None

        link.click()
        wait.until(lambda d: "/course/" in d.current_url)
        course_id = course_id_from_url(driver.current_url)
        if not course_id:
            raise LoginError(f"Could not read a course id out of {driver.current_url}")
        logger.info("Found course id %s", course_id)
        return course_id


def _click(driver, wait: WebDriverWait, locator: tuple, description: str) -> None:
    logger.info("Clicking %s", description)
    wait.until(EC.element_to_be_clickable(locator)).click()


def _visible_courses(driver) -> list[str]:
    try:
        links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/course/"]')
    except WebDriverException:
        return []
    return sorted({link.text.strip() for link in links if link.text.strip()})


def _apply_cookies(session: requests.Session, cookies: list[dict]) -> None:
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
