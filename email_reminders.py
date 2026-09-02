from html import escape


REMINDER_PRESENTATIONS = {
    72: {
        'subject': 'DDL 提醒｜3 天内截止',
        'label': '提前规划',
        'headline': '还有时间，把任务拆开完成',
        'summary': '这项任务已进入 72 小时提醒窗口。现在确认要求并安排进度，会比最后冲刺轻松很多。',
        'accent': '#d6ff62',
        'accent_text': '#101820',
        'soft_background': '#f4fbdc',
        'cta': '查看任务并开始规划',
        'checklist': (
            '确认任务要求和提交格式',
            '拆分任务并安排完成时间',
            '提前准备附件和参考资料',
        ),
    },
    24: {
        'subject': 'DDL 提醒｜24 小时内截止',
        'label': '明天截止',
        'headline': '今天完成，明天从容提交',
        'summary': '这项任务已进入 24 小时提醒窗口。建议今天完成主体内容，并为检查和上传预留时间。',
        'accent': '#facc15',
        'accent_text': '#101820',
        'soft_background': '#fef9c3',
        'cta': '打开任务继续完成',
        'checklist': (
            '今天完成主体内容',
            '检查文件格式、命名和引用',
            '预留时间处理网络或系统问题',
        ),
    },
    3: {
        'subject': '紧急 DDL｜3 小时内截止',
        'label': '需要立即处理',
        'headline': '已进入最后 3 小时',
        'summary': '截止时间已经很近。请暂停非必要任务，立即检查进度、附件和 iSpace 提交入口。',
        'accent': '#f97316',
        'accent_text': '#101820',
        'soft_background': '#fff7ed',
        'cta': '立即检查并提交',
        'checklist': (
            '优先完成可以提交的版本',
            '立即检查附件和提交入口',
            '提交后刷新页面确认状态',
        ),
    },
    1: {
        'subject': '紧急 DDL｜1 小时内截止',
        'label': '最后 1 小时',
        'headline': '现在提交，避免错过截止时间',
        'summary': '任务已进入最后 1 小时。先确保有可用版本成功提交，再继续完善，避免因网络或上传问题错过截止时间。',
        'accent': '#dc2626',
        'accent_text': '#ffffff',
        'soft_background': '#fef2f2',
        'cta': '现在提交',
        'checklist': (
            '先提交当前可用版本',
            '确认附件已经完整上传',
            '看到 iSpace 提交成功后再离开',
        ),
    },
}


def render_todo_reminder_email(
        display_name, title, course, due_time, task_url, site_url,
        reminder_hours, unsubscribe_url=None):
    presentation = REMINDER_PRESENTATIONS.get(
        int(reminder_hours),
        REMINDER_PRESENTATIONS[24],
    )
    subject = f"{presentation['subject']}：{title}"
    checklist_text = '\n'.join(f"- {item}" for item in presentation['checklist'])

    text_body = (
        "MAXCOURSE DDL 提醒\n\n"
        f"{presentation['label']}\n"
        f"{presentation['headline']}\n\n"
        f"{display_name}，你好：\n"
        f"{presentation['summary']}\n\n"
        f"任务：{title}\n"
        f"课程：{course}\n"
        f"截止时间：{due_time} 北京时间 (Beijing Time)\n\n"
        f"建议现在：\n{checklist_text}\n\n"
        f"打开任务：{task_url}\n"
        f"打开 MAXCOURSE：{site_url}\n"
    )
    if unsubscribe_url:
        text_body += f"\n退订邮件提醒 (Unsubscribe)：{unsubscribe_url}\n"

    unsubscribe_html = ''
    if unsubscribe_url:
        unsubscribe_html = (
            f'<p style="margin:24px 0 0;font-size:13px;line-height:1.6;color:#667085;">'
            f'不想再收到提醒？'
            f'<a href="{escape(unsubscribe_url, quote=True)}" style="color:#344054;text-decoration:underline;">一键退订 (Unsubscribe)</a>'
            f'</p>'
        )

    checklist_html = ''.join(
        '<tr>'
        '<td style="width:22px;padding:5px 0;vertical-align:top;color:#101820;font-weight:900;">&#8226;</td>'
        f'<td style="padding:5px 0;color:#344054;font-size:15px;line-height:1.55;">{escape(item)}</td>'
        '</tr>'
        for item in presentation['checklist']
    )
    accent = presentation['accent']
    accent_text = presentation['accent_text']
    soft_background = presentation['soft_background']
    display_name_html = escape(str(display_name))
    title_html = escape(str(title))
    course_html = escape(str(course))
    due_time_html = escape(str(due_time))
    task_url_html = escape(str(task_url), quote=True)
    site_url_html = escape(str(site_url), quote=True)

    html_body = f"""
        <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
            {escape(presentation['summary'])}
        </div>
        <div style="margin:0;padding:0;background:#f4efe6;color:#101820;font-family:Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:#f4efe6;">
                <tr>
                    <td align="center" style="padding:32px 16px 40px;">
                        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:620px;">
                            <tr>
                                <td style="padding:14px 18px;background:{accent};color:{accent_text};border:3px solid #101820;border-bottom:0;">
                                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                                        <tr>
                                            <td style="font-size:12px;font-weight:900;letter-spacing:1.4px;line-height:1.4;">MAXCOURSE · DDL REMINDER</td>
                                            <td align="right" style="font-size:12px;font-weight:900;line-height:1.4;">{escape(presentation['label'])}</td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:28px 24px 24px;background:#ffffff;border:3px solid #101820;box-shadow:8px 8px 0 #101820;">
                                    <p style="margin:0 0 8px;font-size:15px;line-height:1.6;color:#475467;">{display_name_html}，你好</p>
                                    <h1 style="margin:0;font-size:28px;line-height:1.25;letter-spacing:-0.4px;color:#101820;">{escape(presentation['headline'])}</h1>
                                    <p style="margin:14px 0 24px;font-size:16px;line-height:1.7;color:#344054;">{escape(presentation['summary'])}</p>

                                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:{soft_background};border:2px solid #101820;">
                                        <tr>
                                            <td style="padding:20px;">
                                                <p style="margin:0 0 8px;font-size:12px;font-weight:900;letter-spacing:1px;color:#667085;">即将截止</p>
                                                <h2 style="margin:0 0 16px;font-size:21px;line-height:1.35;color:#101820;">{title_html}</h2>
                                                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                                                    <tr>
                                                        <td style="padding:5px 0;width:80px;font-size:14px;font-weight:900;color:#475467;">课程</td>
                                                        <td style="padding:5px 0;font-size:14px;color:#101820;">{course_html}</td>
                                                    </tr>
                                                    <tr>
                                                        <td style="padding:5px 0;width:80px;font-size:14px;font-weight:900;color:#475467;">截止时间</td>
                                                        <td style="padding:5px 0;font-size:14px;font-weight:900;color:#101820;">{due_time_html} 北京时间</td>
                                                    </tr>
                                                </table>
                                            </td>
                                        </tr>
                                    </table>

                                    <p style="margin:24px 0 8px;font-size:14px;font-weight:900;color:#101820;">建议现在</p>
                                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                                        {checklist_html}
                                    </table>

                                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-top:24px;">
                                        <tr>
                                            <td style="background:{accent};border:2px solid #101820;box-shadow:4px 4px 0 #101820;">
                                                <a href="{task_url_html}" style="display:inline-block;padding:13px 18px;color:{accent_text};font-size:15px;font-weight:900;line-height:1.2;text-decoration:none;">{escape(presentation['cta'])}</a>
                                            </td>
                                        </tr>
                                    </table>

                                    <p style="margin:20px 0 0;font-size:13px;line-height:1.6;color:#667085;">
                                        如果任务链接无法打开，请前往 <a href="{site_url_html}" style="color:#101820;font-weight:700;text-decoration:underline;">MAXCOURSE</a> 查看。
                                    </p>
                                    {unsubscribe_html}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:22px 8px 0;text-align:center;font-size:12px;line-height:1.6;color:#667085;">
                                    这是你在 MAXCOURSE 中启用的 DDL 邮件提醒<br>
                                    截止时间统一显示为北京时间 (Beijing Time)
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </div>
    """
    return subject, text_body, html_body
