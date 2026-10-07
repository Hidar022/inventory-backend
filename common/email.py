from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives


def send_product_email(
    *,
    subject,
    recipient,
    text_body,
    greeting,
    heading,
    paragraphs,
    cta_label,
    cta_url,
):
    paragraph_html = "".join(
        '<p style="margin:0 0 18px;color:#334155;font-size:16px;line-height:26px;">'
        f"{escape(paragraph)}</p>"
        for paragraph in paragraphs
    )
    escaped_url = escape(cta_url, quote=True)
    html_body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light">
    <title>{escape(subject)}</title>
    <style>
      @media only screen and (max-width: 600px) {{
        .email-content {{ padding: 30px 22px !important; }}
        .email-footer {{ padding: 20px 22px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#ffffff;color:#0f172a;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;background:#ffffff;">
      <tr>
        <td style="height:3px;background:#2563eb;font-size:0;line-height:0;">&nbsp;</td>
      </tr>
      <tr>
        <td align="center" style="padding:0 16px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:640px;border-collapse:collapse;">
            <tr>
              <td class="email-content" style="padding:38px 36px 34px;">
                <p style="margin:0 0 34px;color:#1d4ed8;font-size:13px;font-weight:700;line-height:18px;">INVENTORY</p>
                <h1 style="margin:0 0 24px;color:#0f172a;font-size:25px;font-weight:700;line-height:32px;">{escape(heading)}</h1>
                <p style="margin:0 0 18px;color:#334155;font-size:16px;line-height:26px;">{escape(greeting)}</p>
                {paragraph_html}
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 22px;border-collapse:collapse;">
                  <tr>
                    <td bgcolor="#1d4ed8" style="border-radius:4px;">
                      <a href="{escaped_url}" style="display:inline-block;padding:13px 19px;color:#ffffff;font-size:14px;font-weight:700;line-height:20px;text-decoration:none;">{escape(cta_label)}</a>
                    </td>
                  </tr>
                </table>
                <p style="margin:0;color:#64748b;font-size:13px;line-height:21px;">If the button doesn't work, copy this link into your browser:<br><a href="{escaped_url}" style="color:#1d4ed8;word-break:break-all;">{escape(cta_url)}</a></p>
              </td>
            </tr>
            <tr>
              <td class="email-footer" style="padding:20px 36px 34px;border-top:1px solid #e2e8f0;color:#64748b;font-size:13px;line-height:21px;">
                Inventory team
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    email = EmailMultiAlternatives(
        subject,
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
    )
    email.attach_alternative(html_body, "text/html")
    email.send()