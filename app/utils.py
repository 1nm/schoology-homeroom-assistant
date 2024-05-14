import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI

PROMPT_TEMPLATE = """
    Summarize the update from my daughter's homeroom teacher.
    List all the action items and information for the parents, group them by actionable and informational.
    Output in markdown formatting.
    ```
    {text}
    ```
"""

def summarize(text):
    prompt = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
    output_parser = StrOutputParser()
    model = ChatOpenAI(model="gpt-4o")
    chain = (
        {"text": RunnablePassthrough()} 
        | prompt
        | model
        | output_parser
    )

    response = chain.invoke(text)
    return response



def send_email(sender_email, receiver_email, bcc_emails, subject, markdown_content):
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

    # Send email through Google's SMTP server
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
        print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

# Example usage
if __name__ == "__main__":
    sender_email = "you@example.com"
    receiver_email = "you@example.com"
    bcc_emails = ["parent@example.com"]
    subject = "Test Email with Markdown"
    markdown_content = """
    # Hello

    This is a **test email** sent from Python using Google's SMTP server with BCC.

    - Item 1
    - Item 2
    - Item 3
    """

    send_email(sender_email, receiver_email, bcc_emails, subject, markdown_content)
