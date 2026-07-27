import unittest

from chunk_md import parse_structured_table


ALARM_TABLE = """
<table>
  <tr><td>告警ID</td><td>告警名称</td><td>告警级别</td><td>产生原因</td><td>处理建议</td></tr>
  <tr><td rowspan="2">200</td><td rowspan="2">直流电路异常</td><td rowspan="2">重要</td><td rowspan="2">内部保护动作。</td><td>先检查外部条件。</td></tr>
  <tr><td>若频繁出现，请联系服务中心。</td></tr>
  <tr><td>告警ID 301</td><td>告警名称</td><td>告警级別</td><td>产生原因</td><td>处理建议</td></tr>
  <tr><td></td><td>电网电压异常</td><td>重要</td><td>电网电压超限。</td><td>检查并网点电压。</td></tr>
</table>
"""


class AlarmTableParsingTests(unittest.TestCase):
    def test_reassembles_rowspan_continuation_and_repeated_header(self):
        parsed = parse_structured_table(ALARM_TABLE)

        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["record_kind"], "alarm")
        records = {record["alarm_id"]: record for record in parsed["structured_table"]["records"]}

        self.assertEqual(set(records), {"200", "301"})
        self.assertIn("若频繁出现", records["200"]["处理建议"])
        self.assertEqual(records["301"]["告警名称"], "电网电压异常")
        self.assertNotIn("告警名称", records["301"]["告警名称"])


if __name__ == "__main__":
    unittest.main()
