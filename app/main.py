import json
import logging
import os
import pickle
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import find_dotenv, load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from utils import send_email, summarize, translate, extract_text_from_pdf

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
        options.add_argument('--disable-search-engine-choice-screen')
        options.add_argument('--disable-gpu')
        if headless:
            options.add_argument("--headless=new")
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
            json.dump(self.config, f, indent=2, ensure_ascii=False)

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

    def download_media(self, url: str, download_path: Path=Path().resolve()) -> Path:
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
                return full_path

            with open(full_path, "wb") as f:
                r.raw.decode_content = True
                shutil.copyfileobj(r.raw, f)

            return full_path

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
            post_datetime = post.find('span', {'class': 'small gray'})
            post_datetime = post_datetime.text if post_datetime else ''

            # Extract author's name and profile picture
            author = post.find('a', {'title': 'View user profile.'})
            author_name = author.text if author else ''

            profile_pic = post.find(
                'img', {'class': 'imagecache imagecache-profile_sm'})
            profile_pic_url = profile_pic.get('src', '') if profile_pic else ''

            # Extract main content
            content_span = post.find('span', {'class': 'update-body s-rte'})
            html_content = content_span.prettify() if content_span else ''

            content = content_span.get_text() if content_span else ''

            # Check if there is a "Show More" link
            show_more_link = post.find('a', {'class': 'show-more-link'})
            show_more_href = show_more_link.get(
                'href', '') if show_more_link else ''

            images = [img.get('src', '')
                      for img in content_span.find_all('img')]

            if show_more_href:
                logging.info(
                    f"Loading additional content for post {post_id} ...")
                with self.session.post(f"{self._base_url}{show_more_href}",
                                       stream=True,
                                       allow_redirects=True) as r:
                    if r.status_code == 200:
                        data = r.json()['update']
                        # Parse the additional content using BeautifulSoup
                        additional_content_soup = BeautifulSoup(
                            data, 'html.parser')
                        html_content = additional_content_soup.prettify()
                        content = additional_content_soup.get_text()
                        images = [
                            img.get('src', '') for img in additional_content_soup.find_all('img')]


            attachments_div = post.find('div', {'class': 'attachments clearfix'})
            
            attachments = []
            
            # Only proceed if attachments_div is found
            if attachments_div:
                attachments_html_content = attachments_div.prettify()

                for a in attachments_div.find_all('a'):
                    href = a.get('href')  # Get the href attribute (attachment link)
                    url = self._base_url + href
        
                    # Find the first <span> child within the <a> tag
                    span = a.find('span')
                    if span:
                        filename = span.get('aria-label')  # Get the aria-label attribute (attachment filename)
                        attachments.append({'url': url, 'filename': filename})
            else:
                attachments_html_content = ''

            for attachment in attachments:
                full_path = str(self.download_media(attachment['url']))
                attachment['full_path'] = full_path
                if full_path.lower().endswith('pdf'):
                    text = extract_text_from_pdf(full_path)
                    attachment['text'] = text

            parsed_posts.append({
                'post_id': post_id,
                'datetime': post_datetime,
                'author': author_name,
                'profile_pic_url': profile_pic_url,
                'content': content.strip(),
                'html_content': html_content,
                'attachments_html_content': attachments_html_content,
                'show_more_href': show_more_href,
                'images': images,
                'attachments': attachments
            })

        return parsed_posts

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
    HOMEROOM_CLASS = os.environ.get("HOMEROOM_CLASS")

    downloader = SchoologyAlbumsDownloader(headless=True, subdomain=SUBDOMAIN)
    downloader.schoology_login(EMAIL, PASSWORD)
    posts = downloader.get_updates()
    for post in reversed(posts):
        if not post['post_id'] in downloader.config['updates']:
            attachment_file_paths = []
            attachments_text = ""
            if post['attachments']:
                for attachment in post['attachments']:
                    attachment_file_paths.append(attachment['full_path'])
                    if attachment['text']:
                        attachments_text += '\n' + attachment['text']

            update_content = f"On {post['datetime']}, {post['author']} posted:\n\n{post['content']}\n\n{attachments_text}"
            summary = summarize(update_content)
            japanese_summary = translate(summary, "Japanese")
            chinese_summary = translate(summary, "Chinese")
            post_date_ymd = ' '.join(post['datetime'].split(' ')[1:4])
            summary_sender_email = os.environ.get("SUMMARY_SENDER_EMAIL")
            bcc_emails_env = os.environ.get("BCC_EMAILS")
            bcc_emails = bcc_emails_env.split(',') if bcc_emails_env else []
            logging.info(f"Sending email to {summary_sender_email} and BCC to {bcc_emails}")


# Construct the markdown content with the attachments section
            markdown_content = (
                f"On {post['datetime']}, {post['author']} posted:" 
                + '\n<br/><br/>\n' 
                + post['html_content'] 
                + '\n<hr/>\n' 
                + summary 
                + '\n<hr/>\n' 
                + japanese_summary 
                + '\n<hr/>\n' 
                + chinese_summary
            )
            send_email(summary_sender_email, summary_sender_email, bcc_emails, f"{HOMEROOM_CLASS} Homeroom Updates", markdown_content, attachment_file_paths)
            downloader.config['updates'][post['post_id']] = post
    downloader._save_config()


if __name__ == "__main__":
    main()
