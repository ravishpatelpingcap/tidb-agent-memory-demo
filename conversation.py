"""The scripted chat from the blog post. No LLM: every run is identical.

The terminal shows the long replies shortened (`agent`), but TiDB saves them in full
(`stored`, read from the replies/ folder). Every token count is estimated the same way,
characters / 4 of the text as saved.
"""
from pathlib import Path

WINDOW = 6000   # a deliberately small context window, so compaction happens quickly

USER_ID, SESSION_ID = "maya", "blog-demo"

REPLIES = Path(__file__).with_name("replies")


def _full(name):
    return (REPLIES / name).read_text(encoding="utf-8")


_INTRO = "Nice to meet you! Let me note that down. So, what are you working on these days?"

TURNS = [
    dict(user="Hi, I'm Maya. My birthday is March 14.",
         agent=_INTRO, stored=_INTRO, note="Maya, birthday March 14"),
    dict(user="I'm a Product Manager. Help me prep the Atlas launch, we ship on Oct 12.",
         agent="On it! Atlas launch prep: messaging doc, pricing page, beta-customer quotes, "
               "and a launch-day checklist... [full plan]",
         stored=_full("atlas_plan.txt"),
         note="Works as a Product Manager", summary="prepped the Atlas launch"),
    dict(user="Summarize this 30-page vendor report.",
         agent="Done. Overall: strong on security and uptime, weak on support SLAs, pricing sits "
               "mid-market. Three contract points need attention before renewal... [30 pages summarized]",
         stored=_full("vendor_report.txt"), summary="summarized the vendor report"),
    dict(user="Plan a 3-day team offsite.",
         agent="Full agenda drafted: day one arrivals and kickoff, day two strategy sessions, "
               "day three workshops and wrap-up... [complete plan]",
         stored=_full("offsite_plan.txt"), summary="planned the 3-day offsite"),
]

QUESTION = "Remind me, when do we ship Atlas?"
FACT = "Oct 12"   # the detail compaction drops
