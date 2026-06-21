"""Redact sensitive tokens from log text before it is published.

The monitor republishes RMS/kernel log lines (last_warning / last_error /
last_watchdog_event / last_oom_line) to a public, anonymous MQTT feed. RMS
`device:` URLs embed camera passwords, and logs carry IPs, usernames and home
paths, so any such line is scrubbed before it leaves the host. Redaction is
deliberately conservative: it removes the high-risk tokens (credentials, IPs,
home-dir usernames) while leaving the component/message readable for triage.
"""

import re

# Value terminators: stop a redacted value at log/URL delimiters so we replace
# only the secret, not the rest of the line (e.g. "...&channel=1").
_VAL = r"[^\s&;,'\"<>)]+"

# Order matters: credential rules run before the IP rule (a URL's userinfo may
# contain an IP/host we still want masked afterwards).
_PATTERNS = [
    # key=value / key: value secrets: password=, passwd=, pwd=, token=, secret=,
    # api_key=, apikey=, auth=, access_key=, AWS_SECRET_KEY= ...  -> keep the key
    # name, mask the value. The leading \w* catches prefixed env-var-style keys
    # (DB_PASSWORD, AWS_SECRET_KEY); the trailing separator requirement keeps it
    # from firing on words that merely contain a keyword (e.g. "author=").
    (re.compile(r"(?i)(\w*(?:pass(?:word|wd)?|pwd|token|secret|auth|"
                r"(?:api|access|secret|private)[_-]?key))(\s*[=:]\s*)" + _VAL),
     r"\1\2***"),
    # URL userinfo: scheme://user:pass@host  ->  scheme://***@host
    (re.compile(r"://[^/\s:@]+(?::[^/\s@]*)?@"), "://***@"),
    # RTSP/ONVIF-style query username: user=admin  ->  user=***
    (re.compile(r"(?i)\buser(\s*[=:]\s*)" + _VAL), r"user\1***"),
    # IPv4 address  ->  <ip>
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    # IPv6 address (>=4 hextets, so it can't swallow an HH:MM:SS timestamp)
    (re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){3,}[0-9A-Fa-f]{1,4}\b"), "<ip6>"),
    # Home-dir username: /home/<user> or /Users/<user>  ->  ~
    (re.compile(r"/(?:home|Users)/[^/\s]+"), "~"),
]


def redact(text):
    """Return `text` with credentials, IPs and home-dir usernames masked.

    None/empty pass through unchanged. Idempotent and allocation-light (a few
    regex subs), so it's fine to call on every published log field each cycle.
    """
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
