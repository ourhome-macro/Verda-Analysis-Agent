"""Conservative claim requirements. Model classification cannot override these guards."""
import re

KINDS = {"declared_fact", "observed_fact", "comparison", "inference"}


def claim_kind(claim):
    text = claim.get("text", "")
    if re.search(r"实际成交|实际支付|实测|真实成交|中保研|碰撞测试|安全评级|安全指数|第三方测试|"
                 r"transaction price|actually paid|measured in practice|crash test|IIHS|Euro.?NCAP|C-IASI", text, re.I):
        return "observed_fact"
    if re.search(r"更安全|更便宜|更可靠|性价比|领先|优于|高于|低于|最安全|最便宜|"
                 r"better than|cheaper than|safer than|outperform|\bsafest\b|\bcheapest\b|\bbest\b|costs (?:less|more) than", text, re.I):
        return "comparison"
    kind = claim.get("verification", {}).get("assessment", {}).get("claim_type")
    return kind if kind in KINDS else "unknown"
