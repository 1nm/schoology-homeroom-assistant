import json
import logging
import os
import pickle
import random
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import find_dotenv, load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from utils import send_email, summarize

logging.basicConfig(format="%(asctime)s - %(levelname)s: %(message)s",
                    level=logging.INFO)


load_dotenv(find_dotenv(usecwd=True))


class SchoologyAlbumsDownloader:

    def __init__(self,
                 timeout: int = 30,
                 headless: bool = True,
                 subdomain: str = "") -> None:
        self._timeout = timeout
        self._base_url = f"https://{subdomain}.schoology.com"
        self.subdomain = subdomain
        self._logger = logging.getLogger(
            SchoologyAlbumsDownloader.__class__.__name__)
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument('--disable-dev-shm-usage')
        if headless:
            options.add_argument("--headless")
        self.driver = webdriver.Chrome(options=options)
        headers = {
            "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        }
        self.session = requests.session()
        self.session.headers.update(headers)
        self._config_file = Path('.sadc.conf')
        self._load_config()

    def __del__(self) -> None:
        pass

    def _save_config(self) -> None:
        self._logger.info(f"Saving config file to {self._config_file}")
        with open(self._config_file, 'w') as f:
            json.dump(self.config, f, indent=2)

    def _load_config(self) -> None:
        if self._config_file.exists():
            self._logger.info(f"Loading config from {self._config_file}")
            with open(self._config_file, 'r') as f:
                self.config = json.load(f)
        else:
            self._logger.info(
                f"Config file not found, initializing default config")
            self.config = {'course_id': '', 'downloaded': {}, 'updates': {}}

    def _save_cookies(self, cookie_file: str) -> None:
        self._logger.info(f"Saving cookies to {cookie_file} ...")
        cookies = self.driver.get_cookies()
        # pickle.dump(cookies, open(cookie_file,"wb"))
        for cookie in cookies:
            if 'expiry' in cookie:
                cookie['expires'] = cookie['expiry']
                del cookie['expiry']
            self.session.cookies.set(cookie['name'],
                                     cookie['value'],
                                     path=cookie['path'])

    def _load_cookies(self, cookie_file: str) -> None:
        self._logger.info(f"Loading cookies from {cookie_file}")
        cookies = pickle.load(open(cookie_file, "rb"))
        self.driver.execute_cdp_cmd('Network.enable', {})
        for cookie in cookies:
            if 'expiry' in cookie:
                cookie['expires'] = cookie['expiry']
                del cookie['expiry']
            self.driver.execute_cdp_cmd('Network.setCookie', cookie)
            self.session.cookies.set(cookie['name'],
                                     cookie['value'],
                                     path=cookie['path'])
        self.driver.execute_cdp_cmd('Network.disable', {})

    def _wait(self, seconds: int) -> None:
        self._logger.info(f"Wait for {seconds} seconds ...")
        time.sleep(seconds)

    def download_media(self, url: str, download_path: Path) -> None:
        with self.session.get(url, stream=True, allow_redirects=True) as r:
            if 'content-disposition' not in r.headers:
                fname = ''.join(
                    random.choices(string.ascii_lowercase + string.digits,
                                   k=16))
            else:
                d = r.headers['content-disposition']
                fname = re.findall('filename="(.+)"', d)[0]
                fname = fname.replace('/', '.')

            full_path = download_path / fname

            self._logger.info(f"Downloading media {url} to {full_path} ...")

            if full_path.exists():
                self._logger.info(f"{full_path} already downloaded, skip ...")
                return

            with open(full_path, "wb") as f:
                r.raw.decode_content = True
                shutil.copyfileobj(r.raw, f)

    def download_album(self, album: tuple[str, str], touch=False) -> None:
        course_id = self.config['course_id']
        (album_url, album_title) = album
        normalized_album_title = album_title.replace('/', '_').replace(':', '')
        album_download_folder = Path(
            f"photos/{course_id}/{normalized_album_title}")
        if album_url in self.config['downloaded']:
            self._logger.info(f"Album {album_url} already downloaded")
            return
        if touch:
            downloaded_at = time.time()
            self._logger.info(
                f"Skip downloading album {normalized_album_title} ...")
        else:
            self._logger.info(
                f"Downloading album {normalized_album_title} from {album_url} ..."
            )
            album_download_folder.mkdir(parents=True, exist_ok=True)
            download_count = 0
            with self.session.get(f"{self._base_url}{album_url}",
                                  stream=True,
                                  allow_redirects=True) as r:
                r.raw.decode_content = True
                result = r.raw.data.decode('unicode-escape').replace(
                    '\\/', '/')
                matches = re.findall(
                    r'(media_albums/m/.*?\.(jpg|jpeg|png|gif|mp4))', result)
                if len(matches) == 0:
                    self._logger.info(
                        f"No matches found on the album page. Loading content pages for download URLs ..."
                    )
                    content_url_matches = re.findall(
                        f"({album_url}/content/[0-9]+)", result)
                    for content_url in content_url_matches:
                        with self.session.get(f"{self._base_url}{content_url}",
                                              stream=True,
                                              allow_redirects=True) as r1:
                            r1.raw.decode_content = True
                            content_page_result = r1.raw.data.decode(
                                'unicode-escape').replace('\\/', '/')
                            download_url_matches = re.findall(
                                r'(media_albums/m/.*?\.(jpg|jpeg|png|gif|mp4))',
                                content_page_result)
                            download_urls = [
                                f"{self._base_url}/system/files/{m[0]}"
                                for m in download_url_matches
                            ]
                            for media_url in download_urls:
                                self.download_media(media_url,
                                                    album_download_folder)
                                download_count += 1
                else:
                    download_urls = [
                        f"{self._base_url}/system/files/{m[0]}"
                        for m in matches
                    ]
                    for photo_url in download_urls:
                        self.download_media(photo_url, album_download_folder)
                        download_count += 1
            downloaded_at = time.time()
            self._logger.info(
                f"Finished downloading album {normalized_album_title} at {downloaded_at} ..."
            )
        if touch or download_count > 0:
            self.config['downloaded'][album_url] = {
                'url': album_url,
                'title': album_title,
                'downloaded_at': downloaded_at
            }
            self._save_config()

    def get_albums(self) -> list[tuple[str, str]]:
        course_id = self.config['course_id']
        albums = []
        page = 0
        while True:
            self._logger.info(f"Loading page {page+1} of the album list...")
            with self.session.get(
                    f"{self._base_url}/course/{course_id}/materials?list_filter=album&ajax=1&style=full&page={page}",
                    stream=True,
                    allow_redirects=True) as r:
                r.raw.decode_content = True
                result = r.raw.data.decode('unicode-escape').replace(
                    '\\/', '/')
                matches = re.findall(r'<a href="(/album/[0-9]+)">(.+?)</a>',
                                     result)
                count = len(matches)
                if count == 0:
                    self._logger.info(
                        f"No match found on page {page+1}, stop loading next page..."
                    )
                    break
                self._logger.info(f"{count} matches found on page {page+1}")
                for match in matches:
                    albums.append(match)
            page += 1
        return albums

    def get_updates(self):
        course_id = self.config['course_id']
        with self.session.get(
                f"{self._base_url}/course/{course_id}/feed?filter=1",
                stream=True,
                allow_redirects=True) as r:
            r.raw.decode_content = True
            result = r.raw.data.decode('unicode-escape').replace(
                '\\/', '/')
            posts = self.parse_posts(result)
            return posts

    def parse_posts(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        posts = soup.find_all('li', {'class': ['first', '']})

        parsed_posts = []

        for post in posts:
            # Extract post id
            post_id = post.get('id', '').replace('edge-assoc-', '')

            # Extract date and time
            datetime = post.find('span', {'class': 'small gray'})
            datetime = datetime.text if datetime else ''

            # Extract author's name and profile picture
            author = post.find('a', {'title': 'View user profile.'})
            author_name = author.text if author else ''

            profile_pic = post.find(
                'img', {'class': 'imagecache imagecache-profile_sm'})
            profile_pic_url = profile_pic.get('src', '') if profile_pic else ''

            # Extract main content
            content_span = post.find('span', {'class': 'update-body s-rte'})
            content = content_span.get_text() if content_span else ''

            # Check if there is a "Show More" link
            show_more_link = post.find('a', {'class': 'show-more-link'})
            show_more_href = show_more_link.get(
                'href', '') if show_more_link else ''

            images = [img.get('src', '')
                      for img in content_span.find_all('img')]

            if show_more_href:
                with self.session.post(f"{self._base_url}{show_more_href}",
                                       stream=True,
                                       allow_redirects=True) as r:
                    if r.status_code == 200:
                        data = r.json()['update']
                        # Parse the additional content using BeautifulSoup
                        additional_content_soup = BeautifulSoup(
                            data, 'html.parser')
                        content = additional_content_soup.get_text()
                        images = [
                            img.get('src', '') for img in additional_content_soup.find_all('img')]

            parsed_posts.append({
                'post_id': post_id,
                'datetime': datetime,
                'author': author_name,
                'profile_pic_url': profile_pic_url,
                'content': content.strip(),
                'html_content': content,
                'show_more_href': show_more_href,
                'images': images
            })

        return parsed_posts

    def onedrive_login(self, email: str, password: str) -> None:
        onedrive_cookie_file = 'onedrive_cookies.pkl'

        if os.path.exists(onedrive_cookie_file) and os.path.isfile(
                onedrive_cookie_file):
            self._load_cookies(onedrive_cookie_file)
            return

        email_flattened = email.replace("@", "_").replace(".", "_")

        self.driver.get(
            f"https://{self.subdomain}-my.sharepoint.com/personal/{email_flattened}/_layouts/15/onedrive.aspx"
        )

        self._wait(5)

        email_input = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.NAME, "loginfmt")))

        next_button = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info("Filling in the email")

        email_input.clear()
        email_input.send_keys(email)
        next_button.click()

        self._wait(5)

        password_input = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.NAME, "passwd")))
        submit_button = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info("Filling in the password")

        password_input.clear()
        password_input.send_keys(password)
        submit_button.click()

        self._wait(5)

        stay_signed_in_button = WebDriverWait(
            self.driver, self._timeout).until(
                EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info(f"Logging in with the email: '{email}'")

        stay_signed_in_button.click()

        self._save_cookies(onedrive_cookie_file)
        self._save_config()

    def schoology_login(self, email: str, password: str) -> None:
        schoology_cookie_file = 'schoology_cookies.pkl'

        if os.path.exists(schoology_cookie_file) and os.path.isfile(
                schoology_cookie_file):
            self._load_cookies(schoology_cookie_file)
            return

        self.driver.get(self._base_url)

        self._logger.info("Loading the login page")

        self._wait(5)

        email_input = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.NAME, "loginfmt")))

        next_button = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info("Filling in the email")

        email_input.clear()
        email_input.send_keys(email)
        next_button.click()

        self._wait(5)

        password_input = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.NAME, "passwd")))
        submit_button = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info("Filling in the password")

        password_input.clear()
        password_input.send_keys(password)
        submit_button.click()

        self._wait(5)

        stay_signed_in_button = WebDriverWait(
            self.driver, self._timeout).until(
                EC.presence_of_element_located((By.ID, "idSIButton9")))

        self._logger.info(f"Logging in with the email: '{email}'")

        stay_signed_in_button.click()

        self._wait(5)

        # Find the button and switch to student account
        drop_down_menu = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, '//img[contains(@alt,"Parents of")]')))

        self._logger.info(f"Swiching to children account ...")

        drop_down_menu.click()

        self._wait(5)

        # Find the button and switch to student account
        switch_child_link = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, '//a[contains(@href,"/parent/switch_child/")]')))

        switch_child_link.click()

        self._wait(5)

        self._logger.info(f"Opening homeroom course ...")
        # Find homeroom link
        homeroom_link = WebDriverWait(self.driver, self._timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, '//a[contains(text(),"Homeroom")]')))

        homeroom_link.click()

        self._wait(5)

        # Find course id
        current_url = str(self.driver.current_url)
        course_id = current_url[36:46]
        self.config['course_id'] = course_id

        self._save_cookies(schoology_cookie_file)

        self.driver.close()



def main():
    EMAIL = os.environ.get("SCHOOLOGY_EMAIL")
    PASSWORD = os.environ.get("SCHOOLOGY_PASSWORD")
    SUBDOMAIN = os.environ.get("SCHOOLOGY_SUBDOMAIN")

    downloader = SchoologyAlbumsDownloader(headless=True,
                                           subdomain=SUBDOMAIN)
    # downloader.onedrive_login(args.email, args.password)
    downloader.schoology_login(EMAIL, PASSWORD)
    posts = downloader.get_updates()
    for post in reversed(posts):
        if not post['post_id'] in downloader.config['updates']:
            update_content = f"On {post['datetime']}, {post['author']} posted:\n\n{post['content']}"
            summary = summarize(update_content)
            today = time.strftime("%Y-%m-%d")
            summary_sender_email = os.environ.get("SUMMARY_SENDER_EMAIL")
            bcc_emails = os.environ.get("BCC_EMAILS").split(',')
            send_email(summary_sender_email, summary_sender_email, bcc_emails, f"Schoology Update Summary {today}", post['html_content'] + '\n<br/><br/>\n' + summary)
            downloader.config['updates'][post['post_id']] = post
    downloader._save_config()


if __name__ == "__main__":
    main()
