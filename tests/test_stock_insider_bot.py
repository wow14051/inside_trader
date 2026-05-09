import base64
import hashlib
import hmac
from pathlib import Path
import sys
import tempfile
import urllib.parse
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stock_insider_bot as bot


FORM4_XML = """
<ownershipDocument>
  <issuer>
    <issuerCik>0000789019</issuerCik>
    <issuerTradingSymbol>MSFT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Jane Doe</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>false</isOfficer>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
        <is10b51Transaction>true</is10b51Transaction>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>60.50</value></transactionPricePerShare>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>25000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionPricePerShare><value>10</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>999999</value></transactionShares>
        <transactionPricePerShare><value>99</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


NON_OFFICER_XML = """
<ownershipDocument>
  <issuer>
    <issuerCik>0000789019</issuerCik>
    <issuerTradingSymbol>MSFT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerRelationship>
      <isDirector>false</isDirector>
      <isOfficer>false</isOfficer>
    </reportingOwnerRelationship>
  </reportingOwner>
</ownershipDocument>
"""


class StockInsiderBotTests(unittest.TestCase):
    def setUp(self):
        bot.set_debug(False)

    def test_parse_options_matches_java_style(self):
        options = bot.parse_options(["AAPL,MSFT", "--threshold=200000", "--debug=false", "--mock"])
        self.assertEqual(options["positional"], "AAPL,MSFT")
        self.assertEqual(options["threshold"], "200000")
        self.assertEqual(options["debug"], "false")
        self.assertEqual(options["mock"], "true")

    def test_short_cli_args_map_to_lookback_and_stock_list(self):
        self.assertEqual(bot.expand_short_cli_args([]), [])
        self.assertEqual(bot.expand_short_cli_args(["7"]), ["--lookback=7"])
        self.assertEqual(
            bot.expand_short_cli_args(["7", "美题", "--threshold=200000"]),
            ["--lookback=7", "--stock-list=美题", "--threshold=200000"],
        )
        self.assertEqual(bot.expand_short_cli_args(["AAPL,MSFT"]), ["AAPL,MSFT"])

    def test_default_lookback_is_seven_days(self):
        self.assertEqual(bot.DEFAULT_MAX_LOOKBACK_DAYS, 7)

    def test_ticker_lookup_normalizes_like_java(self):
        mapping = {"BRK-B": "1067983", "MSFT": "0000789019"}
        self.assertEqual(bot.find_cik_for_ticker("brk.b", mapping), "1067983")
        self.assertEqual(bot.find_cik_for_ticker("MSFT", mapping), "0000789019")
        self.assertIsNone(bot.find_cik_for_ticker("???", mapping))

    def test_extract_tickers_from_plain_stock_list(self):
        text = """
        # my watchlist
        AAPL
        msft
        NASDAQ:NVDA
        BRK-B
        """
        self.assertEqual(
            bot.extract_tickers_from_stock_list_text(text, "watchlist.txt"),
            ["AAPL", "MSFT", "NVDA", "BRK-B"],
        )

    def test_extract_tickers_from_ebk_market_prefix(self):
        text = "31#CRSP\n31#BNTX\n31#TEM\n31#RKLB\n"
        self.assertEqual(
            bot.extract_tickers_from_stock_list_text(text, "美题.ebk"),
            ["CRSP", "BNTX", "TEM", "RKLB"],
        )

    def test_extract_tickers_from_csv_header(self):
        text = "company,ticker\nApple,AAPL\nNvidia,NVDA\nZoetis,ZTS\n"
        self.assertEqual(
            bot.extract_tickers_from_stock_list_text(text, "portfolio.csv"),
            ["AAPL", "NVDA", "ZTS"],
        )

    def test_rejects_regular_text_file(self):
        text = "Meeting notes\nTODO: review the README\nNo stock list here\n"
        self.assertEqual(bot.extract_tickers_from_stock_list_text(text, "notes.txt"), [])

    def test_discover_tickers_from_desktop_imports_multiple_files(self):
        original_desktop_directories = bot.desktop_directories
        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp)
            (desktop / "watchlist.txt").write_text("AAPL\nMSFT\n", encoding="utf-8")
            (desktop / "portfolio.csv").write_text(
                "name,ticker\nNvidia,NVDA\nZoetis,ZTS\n", encoding="utf-8"
            )
            (desktop / "notes.txt").write_text("Meeting notes\nTODO\n", encoding="utf-8")
            try:
                bot.desktop_directories = lambda: [desktop]
                self.assertEqual(
                    bot.discover_tickers_from_desktop(),
                    ["NVDA", "ZTS", "AAPL", "MSFT"],
                )
            finally:
                bot.desktop_directories = original_desktop_directories

    def test_discover_tickers_prefers_project_directory_then_desktop(self):
        original_project_dirs = bot.project_stock_list_directories
        original_desktop_dirs = bot.desktop_directories
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as desktop_tmp:
            project = Path(project_tmp)
            desktop = Path(desktop_tmp)
            (project / "project_watchlist.txt").write_text("AAPL\nMSFT\n", encoding="utf-8")
            (desktop / "desktop_watchlist.txt").write_text("NVDA\nZTS\n", encoding="utf-8")
            try:
                bot.project_stock_list_directories = lambda: [project]
                bot.desktop_directories = lambda: [desktop]
                self.assertEqual(
                    bot.discover_tickers_from_stock_list_files(),
                    (["AAPL", "MSFT"], "project"),
                )
            finally:
                bot.project_stock_list_directories = original_project_dirs
                bot.desktop_directories = original_desktop_dirs

    def test_discover_tickers_falls_back_to_desktop(self):
        original_project_dirs = bot.project_stock_list_directories
        original_desktop_dirs = bot.desktop_directories
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as desktop_tmp:
            project = Path(project_tmp)
            desktop = Path(desktop_tmp)
            (desktop / "desktop_watchlist.txt").write_text("NVDA\nZTS\n", encoding="utf-8")
            try:
                bot.project_stock_list_directories = lambda: [project]
                bot.desktop_directories = lambda: [desktop]
                self.assertEqual(
                    bot.discover_tickers_from_stock_list_files(),
                    (["NVDA", "ZTS"], "Desktop"),
                )
            finally:
                bot.project_stock_list_directories = original_project_dirs
                bot.desktop_directories = original_desktop_dirs

    def test_discover_named_stock_list_prefers_project_and_allows_missing_extension(self):
        original_project_dirs = bot.project_stock_list_directories
        original_desktop_dirs = bot.desktop_directories
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as desktop_tmp:
            project = Path(project_tmp)
            desktop = Path(desktop_tmp)
            (project / "美题.ebk").write_text("31#CRSP\n31#BNTX\n", encoding="utf-8")
            (desktop / "美题.txt").write_text("AAPL\nMSFT\n", encoding="utf-8")
            try:
                bot.project_stock_list_directories = lambda: [project]
                bot.desktop_directories = lambda: [desktop]
                tickers, source = bot.discover_tickers_from_named_stock_list("美题")
                self.assertEqual(tickers, ["CRSP", "BNTX"])
                self.assertTrue(source.endswith("美题.ebk"))
            finally:
                bot.project_stock_list_directories = original_project_dirs
                bot.desktop_directories = original_desktop_dirs

    def test_discover_named_stock_list_falls_back_to_desktop(self):
        original_project_dirs = bot.project_stock_list_directories
        original_desktop_dirs = bot.desktop_directories
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as desktop_tmp:
            project = Path(project_tmp)
            desktop = Path(desktop_tmp)
            (desktop / "美题.csv").write_text("name,ticker\nApple,AAPL\nMicrosoft,MSFT\n", encoding="utf-8")
            try:
                bot.project_stock_list_directories = lambda: [project]
                bot.desktop_directories = lambda: [desktop]
                tickers, source = bot.discover_tickers_from_named_stock_list("美题.csv")
                self.assertEqual(tickers, ["AAPL", "MSFT"])
                self.assertTrue(source.endswith("美题.csv"))
            finally:
                bot.project_stock_list_directories = original_project_dirs
                bot.desktop_directories = original_desktop_dirs

    def test_parse_master_idx_filters_form4_and_ciks(self):
        content = "\n".join(
            [
                "CIK|Company Name|Form Type|Date Filed|Filename",
                "0000789019|MICROSOFT CORP|4|2026-05-01|edgar/data/789019/a.txt",
                "0000789019|MICROSOFT CORP|8-K|2026-05-01|edgar/data/789019/b.txt",
                "0001555285|ZOETIS|4/A|2026-05-01|edgar/data/1555285/c.txt",
            ]
        )
        urls = bot.parse_master_idx(content, {"789019"})
        self.assertEqual(urls, [f"{bot.SEC_BASE}edgar/data/789019/a.txt"])

    def test_parse_form4_extracts_large_purchase_only(self):
        parsed = bot.parse_form4(FORM4_XML, 500_000, {"789019": "MSFT"})
        self.assertEqual(list(parsed), ["MSFT"])
        alerts = parsed["MSFT"]
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.owner_name, "Jane Doe")
        self.assertEqual(alert.position, "CEO")
        self.assertEqual(alert.kind, "BUY")
        self.assertEqual(alert.shares, 10000)
        self.assertEqual(alert.price, 60.5)
        self.assertEqual(alert.amount, 605000)
        self.assertTrue(alert.is_10b5_1)
        self.assertEqual(alert.transaction_date, "2026-05-01")
        self.assertEqual(alert.shares_owned_after, 25000)

    def test_grouped_notification_is_readable_and_highlights_buys(self):
        alert = bot.AlertEntry(
            owner_name="Jane Doe",
            position="CEO",
            kind="BUY",
            shares=10000,
            price=60.5,
            amount=605000,
            is_10b5_1=True,
            transaction_date="2026-05-01",
            shares_owned_after=25000,
        )
        message = bot.build_grouped_notification({"MSFT": [alert]}, "20260508")
        self.assertNotIn("Insider Alerts", message)
        self.assertNotIn("Priority Buys", message)
        self.assertNotIn("Sells ·", message)
        self.assertNotIn("```diff", message)
        self.assertIn("🔴 **MSFT · BUY · $605.0K**", message)
        self.assertIn("- `2026-05-01` · **Jane Doe** · 10b5-1", message)
        self.assertIn("- CEO", message)
        self.assertIn("- `10.0K @ $60.50` · 持仓 `25.0K`", message)

    def test_sell_notification_uses_short_multiline_block(self):
        alert = bot.AlertEntry(
            owner_name="Ye Gang",
            position="COO",
            kind="SELL",
            shares=6800,
            price=85.9,
            amount=584120,
            is_10b5_1=False,
            transaction_date="2026-05-04",
            shares_owned_after=190700,
        )
        message = bot.build_grouped_notification({"SE": [alert]}, "20260508")
        self.assertNotIn("Sells ·", message)
        self.assertIn("**SE · SELL · $584.1K**", message)
        self.assertIn("- `2026-05-04` · Ye Gang", message)
        self.assertIn("- COO", message)
        self.assertIn("- `6.8K @ $85.90` · 持仓 `190.7K`", message)

    def test_notification_groups_by_ticker_and_sorts_groups_by_max_buy_amount(self):
        small_buy = bot.AlertEntry("Buyer A", "CFO", "BUY", 1000, 100, 100000, False, "2026-05-01", 1000)
        big_buy = bot.AlertEntry("Buyer B", "CEO", "BUY", 1000, 900, 900000, False, "2026-05-02", 2000)
        big_sell = bot.AlertEntry("Seller A", "COO", "SELL", 1000, 800, 800000, False, "2026-05-03", 3000)
        small_sell = bot.AlertEntry("Seller B", "CTO", "SELL", 1000, 200, 200000, False, "2026-05-04", 4000)
        message = bot.build_grouped_notification(
            {"AAA": [small_sell, small_buy], "BBB": [big_sell, big_buy]},
            "20260508",
        )
        self.assertLess(message.index("🔴 **BBB · BUY · $900.0K**"), message.index("**BBB · SELL · $800.0K**"))
        self.assertLess(message.index("**BBB · SELL · $800.0K**"), message.index("🔴 **AAA · BUY · $100.0K**"))
        self.assertLess(message.index("🔴 **AAA · BUY · $100.0K**"), message.index("**AAA · SELL · $200.0K**"))
        self.assertIn("持仓 `3.0K`\n\n---\n\n🔴 **AAA", message)

    def test_dingtalk_body_does_not_prepend_visible_title(self):
        original_post_json = bot.post_json
        captured = {}
        try:
            def fake_post_json(url, payload):
                captured["payload"] = payload
                return 200, '{"errcode":0,"errmsg":"ok"}'

            bot.post_json = fake_post_json
            self.assertTrue(bot.send_dingtalk_webhook("https://example.test/webhook", None, "Insider Alert", "**AAPL**"))
            self.assertEqual(captured["payload"]["markdown"]["text"], "**AAPL**")
        finally:
            bot.post_json = original_post_json

    def test_discord_body_does_not_prepend_visible_title(self):
        original_send_single = bot.send_single_discord_message
        captured = {}
        try:
            def fake_send_single(webhook_url, content):
                captured["content"] = content
                return True

            bot.send_single_discord_message = fake_send_single
            self.assertTrue(bot.send_discord_messages("https://example.test/webhook", "Insider Alert", "**AAPL**"))
            self.assertEqual(captured["content"], "**AAPL**")
        finally:
            bot.send_single_discord_message = original_send_single

    def test_missing_notification_matches_java_style(self):
        message = bot.build_missing_notification(["AAPL", "MSFT"], "No Form 4 filings found")
        self.assertEqual(
            message,
            "🔔 Insider Alerts\n\n"
            "▶ AAPL\n  No Form 4 filings found\n\n"
            "▶ MSFT\n  No Form 4 filings found",
        )

    def test_parse_form4_skips_non_officer_or_director(self):
        self.assertEqual(bot.parse_form4(NON_OFFICER_XML, 1, {"789019": "MSFT"}), {})

    def test_extract_xml_payload_from_sec_wrapper(self):
        wrapped = f"<SEC-DOCUMENT><XML>{FORM4_XML}</XML></SEC-DOCUMENT>"
        self.assertTrue(bot.extract_xml_payload(wrapped).startswith("<ownershipDocument>"))

    def test_dingtalk_signature(self):
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=abc"
        secret = "this is secret"
        timestamp = 123456789
        signed = bot.build_dingtalk_url(webhook, secret, timestamp)
        expected_digest = hmac.new(
            secret.encode(),
            f"{timestamp}\n{secret}".encode(),
            hashlib.sha256,
        ).digest()
        expected_sign = urllib.parse.quote_plus(base64.b64encode(expected_digest).decode())
        self.assertEqual(signed, f"{webhook}&timestamp={timestamp}&sign={expected_sign}")


if __name__ == "__main__":
    unittest.main()
