"""The scripted chat from the blog post. No LLM: every run is identical.

Each exchange carries a fixed token budget, the size a real reply would be. The long
replies themselves are one-line stand-ins, so the rows stored in TiDB are small.
"""

WINDOW = 6000   # a deliberately small context window, so compaction happens quickly

USER_ID, SESSION_ID = "maya", "blog-demo"

TURNS = [
    dict(user="Hi, I'm Maya. My birthday is March 14.",
         agent="Nice to meet you! Let me note that down. So, what are you working on these days?",
         budget=34, note="Maya, birthday March 14"),
    dict(user="I'm a Product Manager. Help me prep the Atlas launch, we ship on Oct 12.",
         agent="On it! Atlas launch prep: messaging doc, pricing page, beta-customer quotes, "
               "and a launch-day checklist... [full plan]",
         budget=420, note="Works as a Product Manager", summary="prepped the Atlas launch"),
    dict(user="Summarize this 30-page vendor report.",
         agent="Done. Overall: strong on security and uptime, weak on support SLAs, pricing sits "
               "mid-market. Three contract points need attention before renewal... [30 pages summarized]",
         budget=3900, summary="summarized the vendor report"),
    dict(user="Plan a 3-day team offsite.",
         agent="Full agenda drafted: day one arrivals and kickoff, day two strategy sessions, "
               "day three workshops and wrap-up... [complete plan]",
         budget=3200, summary="planned the 3-day offsite"),
]

QUESTION = "Remind me, when do we ship Atlas?"
FACT = "Oct 12"   # the detail compaction drops
