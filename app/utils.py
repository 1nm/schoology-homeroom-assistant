import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import markdown2
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI

import fitz  # PyMuPDF

PROMPT_TEMPLATE = """
    Summarize the update from my daughter's homeroom teacher.

    List all the action items and information for the parents, grouping them into 'Action Items' and 'Information'.

    Output in the following markdown formatting:

    - The document starts with a title "AI Summary", formatted with a single '#'.
    - The content is divided into two sections: "Action Items" and "Information", each marked with '##'.
    - Under each section, list the items as numbered bullet points (1., 2., etc.). Each item should have a bolded item name followed by a colon.
    - For each item, include any specific details or actions as sub-points, using unnumbered bullet points starting with a hyphen (-). **Do not number the sub-points.**
    - Ensure that only the main items are numbered, and sub-points are indented and use hyphens without numbers.
    - Ensure that there is no strikethrough formatting in the markdown.
    - Replace any HTML-like tags such as <sh>, <ch>, <s>, and <x> with double quotation marks "sh", "ch", "s" and "x".

    Example:
    ```
    # AI Summary
    ## Action Items
    1. **UOI Animal Book Presentation Sign-Up:**
        - Choose three available slots (June 4, 5, 6 at 8:10-8:25 or 15:30-15:45) and email them to the teacher.
        - If selecting a morning slot, come to school with your child.
    2. **Field Trip to the Zoo:**
        - Prepare for the trip and ensure your child is ready for the outing.

    ## Information
    1. **Mother's Day:**
        - A secret present is in the children's backpacks made by them.
    2. **Upcoming Events:**
        - May 17 and 24: Riverside Park visits
    ```

    Update from Homeroom Teacher:
    ```
    {text}
    ```

    Summary in markdown format, no triple backticks:
"""

TRANSLATION_PROMPT_TEMPLATE = """
    Translate the following content into {language}, keep the original markdown formatting.
    For English to Japanese, translate "AI Summary" to "AI 概要", "Action Items" to "アクションアイテム", "Information" to "情報", "fact families" to "ファクトファミリー"
    For English to Chinese, translate "AI Summary" to "AI 总结", "Action Items" to "行动项目", "Information" to "信息", "fact families" to "fact families".
    
    ```
    {content}
    ```

    Translation in markdown format, no triple backticks:
"""

def summarize(text):
    prompt = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
    output_parser = StrOutputParser()
    model = ChatOpenAI(model="gpt-4.1")
    chain = (
        prompt
        | model
        | output_parser
    )

    response = chain.invoke({'text': text})
    return response

def translate(markdown_content, language):
    """
    Translate Markdown content to the specified language.

    Parameters:
    - markdown_content: Content in Markdown format.
    - language: Target language for translation.

    Returns:
    - Translated content in Markdown format.
    """
    # Set up the translation client
    prompt = ChatPromptTemplate.from_template(TRANSLATION_PROMPT_TEMPLATE)
    output_parser = StrOutputParser()
    model = ChatOpenAI(model="gpt-4o")
    chain = (
        prompt
        | model
        | output_parser
    )

    response = chain.invoke({'content': markdown_content, 'language': language})
    return response


def markdown_to_html(markdown_content):
    html_content = markdown2.markdown(markdown_content)
    logging.info(markdown_content)
    logging.info(html_content)
    return html_content

def send_email(sender_email, receiver_email, bcc_emails, subject, html_content, attachment_file_paths):
    """
    Send an email with Markdown content converted to HTML.

    Parameters:
    - sender_email: Email address of the sender.
    - receiver_email: Email address of the receiver.
    - bcc_emails: List of BCC email addresses.
    - subject: Subject of the email.
    - markdown_content: Content in Markdown format.
    - app_password_env_var: Environment variable name where the app password is stored (default is 'GOOGLE_APP_PASSWORD').

    Returns:
    - None
    """
    # Read app password from environment variable
    app_password = os.getenv('GOOGLE_APP_PASSWORD')
    if not app_password:
        raise ValueError("App password not found in environment variables.")

    # Create the email message
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject
    msg['Bcc'] = ", ".join(bcc_emails)

    # Attach the HTML content
    msg.attach(MIMEText(html_content, 'html'))

    # Combine all recipient emails
    all_recipients = [receiver_email] + bcc_emails

    for file_path in attachment_file_paths:
        with open(file_path, "rb") as attachment:
            # Instance of MIMEBase and named as part
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
            # Encode file in ASCII characters to send by email    
            encoders.encode_base64(part)
            # Add header as key/value pair to attachment part
            part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(file_path)}')
            # Attach the instance 'part' to instance 'msg'
            msg.attach(part)

    # Send email through Google's SMTP server
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
        logging.info("Email sent successfully!")
    except Exception as e:
        logging.info(f"Failed to send email: {e}")


def extract_text_from_pdf(pdf_path):
    document = fitz.open(pdf_path)
    all_text = ''
    for page in document:
        all_text += page.get_text() + '\n'  # Extracts text from each page
    document.close()
    return all_text


def notify_error(error_message):
    sender_email = os.environ.get("SUMMARY_SENDER_EMAIL")
    if not sender_email:
        logging.error("SUMMARY_SENDER_EMAIL not set, cannot send error email.")
        return

    subject = "Schoology Script Failed"
    html_content = f"<h3>The script failed with the following error:</h3><pre>{error_message}</pre>"

    logging.info(f"Sending error email to {sender_email}")
    send_email(
        sender_email,
        sender_email,
        [],
        subject,
        html_content,
        [],
    )
