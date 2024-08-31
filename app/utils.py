import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import markdown
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI

import pdfplumber

PROMPT_TEMPLATE = """
    Summarize the update from my daughter's homeroom teacher.
    List all the action items and information for the parents, group them by action items and information.
    Output in the following example markdown formatting.

    The document starts with a title "AI Summary", formatted with a #.
    Each update summary is divided into two sections: "Action Items" and "Information", each marked with a ##.
    Under "Action Items", there are numbered bullet points (1., 2., etc.), each containing a bolded item name.
    Each bolded item name has lower-level bullet points (-) with specific details or actions.
    Under "Information", there are also numbered bullet points, each containing a bolded item name.
    Each bolded item name under "Information" can also have lower-level bullet points with additional details.

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
        - A secret present is in the children’s backpacks made by them.
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
    
    ```
    {content}
    ```

    Translation in markdown format, no triple backticks:
"""

def summarize(text):
    prompt = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
    output_parser = StrOutputParser()
    model = ChatOpenAI(model="gpt-4o")
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


def send_email(sender_email, receiver_email, bcc_emails, subject, markdown_content, attachment_file_paths):
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

    # Convert Markdown to HTML
    html_content = markdown.markdown(markdown_content)

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
    all_text = ''
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            all_text += page.extract_text() + '\n'  # Extracts text from each page
    return all_text

