import os
import unittest

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')

import app as app_module


class EmailReminderTestCase(unittest.TestCase):
    def test_todo_reminder_email_has_distinct_urgency_designs(self):
        user = {
            'username': 'notify-user',
            'ispace_username': None,
            'display_name': 'Notify User',
        }
        todo = {
            'title': 'Submit essay',
            'course': 'WRIT1001',
            'due_date': 1700000000,
            'url': 'https://ispace.bnbu.edu.cn/mod/assign/view.php?id=1',
        }
        expectations = {
            72: ('DDL 提醒｜3 天内截止', '提前规划', '#d6ff62', '查看任务并开始规划'),
            24: ('DDL 提醒｜24 小时内截止', '明天截止', '#facc15', '打开任务继续完成'),
            3: ('紧急 DDL｜3 小时内截止', '需要立即处理', '#f97316', '立即检查并提交'),
            1: ('紧急 DDL｜1 小时内截止', '最后 1 小时', '#dc2626', '现在提交'),
        }

        subjects = set()
        for hours, (subject_prefix, urgency_label, accent, cta) in expectations.items():
            with self.subTest(hours=hours):
                subject, text_body, html_body = app_module.build_todo_reminder_email(
                    user,
                    todo,
                    hours,
                    unsubscribe_url='https://www.bnbscheduler.top/unsubscribe-test',
                )
                subjects.add(subject)
                self.assertTrue(subject.startswith(subject_prefix))
                self.assertIn(urgency_label, text_body)
                self.assertIn('北京时间', text_body)
                self.assertIn('Unsubscribe', text_body)
                self.assertIn(accent, html_body.lower())
                self.assertIn(cta, html_body)
                self.assertIn(urgency_label, html_body)

        self.assertEqual(len(subjects), 4)


if __name__ == '__main__':
    unittest.main()
