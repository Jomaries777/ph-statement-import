"""
Build docs/design/gcp-architecture.drawio: the hosted app's Google Cloud architecture.

The icons from docs/icons are embedded in the file, so the .drawio opens on its
own in diagrams.net or the VS Code Draw.io extension. Edit this script and run
it again rather than editing the .drawio by hand, so the next change is easy too.

Usage:
    python docs/design/tools/make_drawio.py docs/icons docs/design/gcp-architecture.drawio

Check the result parses:
    python -c "import xml.dom.minidom,sys; xml.dom.minidom.parse(sys.argv[1])" docs/design/gcp-architecture.drawio
"""

import base64
import os
import sys
from xml.sax.saxutils import escape, quoteattr

ICON_DIR, OUT = sys.argv[1], sys.argv[2]

# =============================================================================
# DRAWING HELPERS
# =============================================================================


def icon_uri(name):
    """docs/icons/<name> as a data URI that draw.io can show."""
    with open(os.path.join(ICON_DIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode()
    kind = "svg+xml" if name.endswith(".svg") else "png"
    return f"data:image/{kind},{data}"  # no ';base64': ';' separates draw.io style keys


cells = []
next_id = [1]


def new_id():
    next_id[0] += 1
    return f"c{next_id[0]}"


def vertex(value, x, y, w, h, style, parent="1"):
    cell_id = new_id()
    cells.append(
        f'<mxCell id="{cell_id}" value={quoteattr(value)} style={quoteattr(style)} vertex="1" parent="{parent}">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )
    return cell_id


def edge(source, target, label="", new=False, points=(), style_extra=""):
    """An arrow. New parts are dashed; `points` are bends, to keep lines off labels."""
    style = (
        "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=blockThin;endFill=1;endSize=7;"
        "fontSize=11;fontColor=#3C4043;strokeColor=#80868B;strokeWidth=1.25;labelBackgroundColor=#FFFFFF;"
        + ("dashed=1;dashPattern=5 4;" if new else "")
        + style_extra
    )
    bends = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in points)
    bends = f'<Array as="points">{bends}</Array>' if bends else ""
    cells.append(
        f'<mxCell id="{new_id()}" value={quoteattr(label)} style={quoteattr(style)} edge="1" parent="1" '
        f'source="{source}" target="{target}"><mxGeometry relative="1" as="geometry">{bends}</mxGeometry></mxCell>'
    )


def card(title, note, icon, x, y, w=210, h=64, new=False, wordmark=False):
    """White card: icon on the left, bold title and a grey note. New parts get a dashed blue border."""
    label = f'<b>{escape(title)}</b><br><font style="font-size:10px" color="#5F6368">{note}</font>'
    icon_w = 52 if wordmark else 40
    style = (
        "rounded=1;arcSize=12;absoluteArcSize=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;shadow=1;"
        "align=left;verticalAlign=middle;fontSize=12;fontColor=#202124;fontFamily=Helvetica;"
        f"spacingLeft={icon_w + 20};"
        + ("strokeColor=#1A73E8;dashed=1;dashPattern=6 4;strokeWidth=1.5;" if new else "strokeColor=#DADCE0;strokeWidth=1;")
    )
    card_id = vertex(label, x, y, w, h, style)
    icon_h = 13 if wordmark else 40
    vertex(
        "", 12, (h - icon_h) / 2, icon_w, icon_h,
        f"shape=image;image={icon_uri(icon)};imageAspect=1;aspect=fixed;connectable=0;",
        parent=card_id,
    )
    return card_id


def group(title, x, y, w, h, color, fill):
    vertex(
        title, x, y, w, h,
        f"rounded=1;arcSize=16;absoluteArcSize=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={color};"
        "verticalAlign=top;align=left;spacingLeft=14;spacingTop=8;fontSize=13;fontStyle=1;"
        f"fontColor={color};fontFamily=Helvetica;",
    )


# =============================================================================
# 1. TITLE AND GROUPS (drawn first, so they sit behind the cards)
# =============================================================================

vertex(
    '<b style="font-size:20px">ph-statement-import · GCP architecture</b><br>'
    '<font color="#5F6368">Hosted mode: OAuth-only manual upload + opt-in Gmail auto-import · '
    "free tier only, $0 a month</font>",
    40, 6, 1000, 56, "text;html=1;align=left;verticalAlign=top;fontSize=12;fontFamily=Helvetica;",
)
group("GitHub · public repo", 300, 70, 650, 130, "#3C4043", "#F8F9FA")
group("GCP project · us-west1 (Oregon) · free tier only", 240, 230, 950, 690, "#1A73E8", "#F4F8FE")
group("External services", 1240, 300, 300, 400, "#3C4043", "#F8F9FA")

# =============================================================================
# 2. CARDS
# =============================================================================

browser = card("Browser", "you, family<br>(later: public)", "user.svg", 30, 450, w=180)

# GitHub
repo = card("Repo · main branch", "branch protection, PRs only<br>required: tests + lint", "git-repo.svg", 320, 112, w=250, h=72)
actions = card(
    "GitHub Actions",
    "tests.yml: tests + lint (every PR)<br>deploy.yml (main only):<br>build image → push → deploy",
    "devops.svg", 670, 108, w=260, h=80, new=True,
)

# GCP, top row
registry = card("Artifact Registry", "container image<br>keeps 2 newest (0.5 GB free)", "artifact-registry.svg", 260, 280, w=210, new=True)
wif = card("Workload Identity Fed.", "GitHub OIDC, main only<br>no JSON keys", "workload-identity.svg", 500, 280, w=220, new=True)
secret = card(
    "Secret Manager", "app-config: one JSON secret<br>Flask key, OAuth secrets,<br>encryption keys",
    "secret-manager.svg", 750, 276, w=205, h=72,
)
logging = card("Cloud Logging", "request logs, no secrets<br>default 30-day retention", "logging.svg", 975, 280, w=200)

# GCP, the app
run = card(
    "Cloud Run · Flask app",
    "gunicorn · 1 worker · 8 threads<br>min 0 · max 1 · CPU only in requests<br>"
    "manual upload: in memory only<br>/tasks/poll-gmail (OIDC)",
    "cloud-run.svg", 520, 450, w=310, h=112,
)

# GCP, auto-import
scheduler = card("Cloud Scheduler", "every 15 min · 1 of 3 free jobs<br>OIDC → /tasks/poll-gmail", "scheduler.svg", 260, 650, w=230, new=True)
firestore = card(
    "Firestore (default)",
    "users/{sub}: plan + wallet→account<br>tokens encrypted in the app<br>└ runs/{msg}:{source}",
    "firestore.svg", 540, 646, w=270, h=72, new=True,
)

# GCP, cost guard
budget = card("Budget $1 / ₱50", "alerts at 50 · 90 · 100%<br>on actual spend", "billing.svg", 260, 810, w=230, new=True)
pubsub = card("Pub/Sub", "topic budget-alerts", "pubsub.svg", 540, 810, w=200, new=True)
guard = card(
    "budget-guard · Cloud Run",
    "same image, private<br>over budget → disables billing",
    "cloud-run.svg", 800, 810, w=260, new=True,
)

# External
ynab = card("API + OAuth", "api.ynab.com · app.ynab.com<br>read/write scope", "ynab.png", 1270, 340, w=240, wordmark=True)
gauth = card("Google OAuth", "accounts.google.com<br>openid email profile<br>gmail.readonly", "oauth.svg", 1270, 466, w=240, h=72, new=True)
gmail = card("Gmail API", "search per wallet<br>get PDF attachment", "gmail.svg", 1270, 600, w=240, new=True)

# =============================================================================
# 3. ARROWS
# =============================================================================

# Users
edge(browser, run, "HTTPS: page, /api/*, PDF upload")
edge(browser, ynab, "OAuth redirect", points=[(84, 950), (1525, 950), (1525, 372)],
     style_extra="exitX=0.3;exitY=1;exitDx=0;exitDy=0;")
edge(browser, gauth, "OAuth redirect", new=True, points=[(138, 970), (1532, 970), (1532, 502)],
     style_extra="exitX=0.6;exitY=1;exitDx=0;exitDy=0;")

# The app's calls
edge(run, ynab, "transactions, balances", style_extra="exitX=1;exitY=0.25;")
edge(run, gauth, "token exchange / refresh", new=True, style_extra="exitX=1;exitY=0.55;")
edge(run, gmail, "list messages, get PDF", new=True, points=[(1210, 555), (1210, 632)],
     style_extra="exitX=1;exitY=0.94;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;")
edge(run, secret, "secrets at start", style_extra="exitX=0.75;exitY=0;")
edge(run, logging, "", style_extra="exitX=1;exitY=0;")
edge(run, firestore, "settings, run log (encrypted fields)", new=True)
edge(scheduler, run, "POST /tasks/poll-gmail", new=True, style_extra="exitX=0.5;exitY=0;entryX=0;entryY=0.75;")

# Deploys
edge(repo, actions, "push")
edge(actions, wif, "OIDC token", new=True)
edge(wif, registry, "push", new=True)  # short: the gap between the cards is narrow
edge(registry, run, "deploy revision", new=True, style_extra="entryX=0;entryY=0.25;")

# Cost guard
edge(budget, pubsub, "budget messages", new=True)
edge(pubsub, guard, "push (OIDC)", new=True)
edge(guard, budget, "unlink billing", new=True, points=[(930, 900), (375, 900)],
     style_extra="exitX=0.5;exitY=1;exitDx=0;exitDy=0;entryX=0.5;entryY=1;entryDx=0;entryDy=0;")

# =============================================================================
# 4. LEGEND
# =============================================================================

vertex(
    "<b>Legend</b><br>"
    "Grey border: exists today or needed for manual upload<br>"
    "Dashed blue border / dashed arrow: new for deploys, Gmail auto-import or the cost guard<br><br>"
    "<b>Free tier ($0 a month)</b><br>"
    "Cloud Run: us-west1 for the free 1 GB outbound from North America; min 0, max 1<br>"
    "One secret (10k accesses), 2 images (0.5 GB), 1 Scheduler job, Firestore (default) only<br>"
    "No Cloud KMS, Cloud Build, load balancer or custom domain<br><br>"
    "<b>Limits</b><br>"
    "YNAB OAuth Restricted Mode: 25 users until YNAB review<br>"
    "Google unverified app (gmail.readonly): up to 100 users; ALLOWED_GOOGLE_EMAILS gates auto-import<br><br>"
    '<font color="#5F6368">Icons: docs/icons. Artifact Registry, Workload Identity, Cloud Logging, budget,<br>'
    "Google OAuth, repo and user are stand-ins drawn in the same style.</font>",
    240, 1000, 640, 250,
    "rounded=1;arcSize=12;absoluteArcSize=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#DADCE0;"
    "align=left;verticalAlign=top;spacingLeft=12;spacingTop=8;fontSize=11;fontFamily=Helvetica;",
)

# =============================================================================
# 5. WRITE THE FILE
# =============================================================================

xml = (
    '<mxfile host="app.diagrams.net" type="device">'
    '<diagram id="gcp-architecture" name="GCP architecture">'
    '<mxGraphModel dx="1600" dy="1300" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" '
    'fold="1" page="1" pageScale="1" pageWidth="1600" pageHeight="1300" math="0" shadow="0">'
    '<root><mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n"
)
with open(OUT, "w") as f:
    f.write(xml)
print(f"wrote {OUT}: {len(cells)} cells, {len(xml):,} bytes")
