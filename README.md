<a id="readme-top"></a>

<!-- PROJECT SHIELDS -->
<div align="center">

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![Latest Release][release-shield]][release-url]
[![Downloads][downloads-shield]][downloads-url]

</div>

<!-- PROJECT LOGO -->
<br />
<div align="center">
  <h3 align="center">Mailbot</h3>

  <p align="center">
    Cold outreach to early-stage startups that finds and verifies real contacts, using only free data sources.
    <br />
    <a href="https://github.com/teddycitrus/mailbot"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="https://github.com/teddycitrus/mailbot/releases/latest/download/mailbot.exe"><strong>Download for Windows »</strong></a>
    &middot;
    <a href="https://github.com/teddycitrus/mailbot/issues/new?labels=bug">Report Bug</a>
    &middot;
    <a href="https://github.com/teddycitrus/mailbot/issues/new?labels=enhancement">Request Feature</a>
  </p>
</div>

<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#how-it-finds-addresses">How It Finds Addresses</a></li>
        <li><a href="#guardrails">Guardrails</a></li>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
        <li><a href="#building-from-source">Building From Source</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#configuration">Configuration</a></li>
    <li><a href="#scheduling">Scheduling</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>

<!-- ABOUT THE PROJECT -->
## About The Project

Mailbot finds people worth emailing at early-stage startups, works out their
address, checks the address actually exists, writes a personalised note, and
sends a small number of them each morning. It then reads the replies, stops
contacting anyone who asks it to, and drafts an answer to each reply for you to
review and send yourself.

Commercial tools in this space charge for the same pipeline. The expensive part
of those products is not the technique, it is the proprietary contact corpus and
the pool of IP addresses used to verify against. Mailbot builds its own corpus
as it goes and stays deliberately slow, which costs nothing.

It runs entirely on your machine. There is no account, no server, and no
telemetry. Your credentials, your resume and your contact database never leave
the computer you run it on.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### How It Finds Addresses

Five free sources, in the order they are tried:

| Source | What it gives | Cost |
| --- | --- | --- |
| Y Combinator OSS directory | Companies, location, headcount, batch, hiring flag | No key |
| GitHub org search by city | Companies that never applied to an accelerator | Token |
| Company website crawl | Any address the company publishes itself | No key |
| Public git commits | Addresses engineers published in their own commits | Optional token |
| Hacker News "Who is hiring" | Addresses the hiring person typed themselves | No key |

Discovery runs on two sources because one accelerator is not a market. YC's
directory is roughly thirty San Francisco companies for every Toronto one, which
describes YC rather than the cities. GitHub org search is keyed on a
self-reported location, so it reaches companies outside that pipeline and gives
every target city comparable depth. The tradeoff is metadata: a YC record
carries a batch and a team size, a GitHub org carries neither, so the age filter
falls back to the org's creation date and the headcount filter cannot run.
Companies are tagged with the source they came from and stay distinguishable.

When no address is published, one is inferred from the person's name and the
domain's observed naming convention, then confirmed over SMTP: an MX lookup,
then `EHLO`, `MAIL FROM` and `RCPT TO`, closing the connection before `DATA` so
nothing is ever delivered during a check.

Every confirmed address teaches the tool that domain's convention, so guessing
gets more accurate the longer it runs.

There is one honest limit. Most startups use Google Workspace, which answers
`250 OK` to every recipient including invented ones, so a probe there proves
nothing. On those domains Mailbot will not send to a guess. It falls back to an
address a human actually published, or it skips the company.

### Guardrails

This is a cold email tool, so the restraint is the point:

- A hard daily cap, defaulting to a deliberately low number
- An address is contacted at most once, enforced by a unique index and a send log
- One person per company, so it never looks like a blast
- Sends only during the recipient's own local morning, on weekdays
- Replies asking to stop are honoured automatically, before the next send
- Answers to replies are only ever saved as drafts, never sent by the tool
- Bounced addresses are suppressed on the next run
- At most one follow-up, ever
- Nothing is claimed about you except what you list in `config/aspects.txt`

### Built With

* [Python 3](https://www.python.org/) standard library, plus `requests`, `dnspython` and `beautifulsoup4`
* [React](https://react.dev/) and [TypeScript](https://www.typescriptlang.org/)
* [Vite](https://vitejs.dev/), [Tailwind CSS](https://tailwindcss.com/) and [Recharts](https://recharts.org/)
* [SQLite](https://www.sqlite.org/) for local state
* [PyInstaller](https://pyinstaller.org/) for the packaged build

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->
## Getting Started

### Prerequisites

* A mail account you can create an app password for. Gmail needs two-factor
  authentication enabled first.
* Your resume as a PDF. The filename travels with the email, so name it
  after yourself rather than leaving it as resume.pdf.
* Nothing else. Both API integrations are optional and both have free tiers.

### Installation

Download the latest build:

    https://github.com/teddycitrus/mailbot/releases/latest/download/mailbot.exe

That link always serves the newest release, so it is safe to bookmark or share.
Every version is also listed on the
[releases page](https://github.com/teddycitrus/mailbot/releases).

Put the executable in a folder of its own and run it. The console opens in your
browser and walks you through setup.

Windows will warn that the publisher is unrecognised, because the build is not
code signed. Choose "More info" and then "Run anyway" if you are willing to
trust it, or build it yourself from source using the steps below.

The executable creates its configuration, database and resume folder beside
itself. Keep it in its own directory rather than a shared Downloads folder.

### Building From Source

```sh
git clone https://github.com/teddycitrus/mailbot.git
cd mailbot
pip install -r requirements.txt

cd dashboard && npm install && npm run build && cd ..
python -m src.main dashboard
```

To produce the executable:

```sh
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
python -m src.main verify-build
```

`verify-build` scans the finished binary for your credentials, your email
address, your resume and your contact database, and refuses to pass if it finds
any of them. Run it before publishing a build.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE -->
## Usage

The console is the intended interface. On first run it shows a setup checklist;
once the required steps are done it becomes a progress dashboard with reply
rates, bounce rates, queue depth and the full contact table.

Every stage is also a command, which is what the scheduled jobs call:

```sh
python -m src.main dashboard      # setup and progress console
python -m src.main discover       # find qualifying companies (Y Combinator)
python -m src.main gh             # find companies via GitHub org search
python -m src.main hn             # import the Hacker News hiring threads
python -m src.main priority       # seed the companies named in config/priority.txt
python -m src.main enrich         # find and verify a contact at each company
python -m src.main queue          # render personalised drafts
python -m src.main mirror         # copy queued drafts into Gmail Drafts
python -m src.main preview        # read the drafts before anything is sent
python -m src.main send           # send, respecting cap and window
python -m src.main followup       # one nudge to people who never replied
python -m src.main inbox          # scan for bounces, replies and opt-outs, draft answers
python -m src.main digest         # email yourself a weekly summary
python -m src.main doctor         # check everything the scheduled jobs rely on
```

Sending stays off until you set `DRY_RUN=false`. Run `preview` first and read
what it is about to send in your name.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONFIGURATION -->
## Configuration

The console writes these for you. They live in `.env` beside the executable and
are never committed or bundled.

| Key | Default | Notes |
| --- | --- | --- |
| `FROM_NAME`, `FROM_EMAIL` | none | Your identity. Reply-to and unsubscribe derive from these |
| `SMTP_PASS`, `IMAP_PASS` | none | App password, not your account password |
| `DAILY_SEND_LIMIT` | `10` | A personal address should stay well under this kind of volume |
| `SEND_WINDOW_START`, `SEND_WINDOW_END` | `08:30`, `10:00` | Local to each recipient |
| `PER_RECIPIENT_TIMEZONE` | `true` | Send during their morning, not yours |
| `MIN_CONFIDENCE` | `70` | Below this an address is never queued |
| `QUEUE_TARGET` | `60` | How many finished drafts to keep waiting |
| `MIRROR_TO_DRAFTS` | `true` | Copy queued drafts to Gmail Drafts as a fallback |
| `PRIORITY_PATH` | `config/priority.txt` | Companies that skip the gates and go first |
| `TARGET_LOCATIONS` | `San Francisco,New York,Toronto` | Comma separated |
| `MAX_EMPLOYEES`, `MAX_COMPANY_AGE_YEARS` | `200`, `5` | Target filter |
| `PREFER_RECENTLY_FUNDED` | `true` | Rank by recency of funding blended with ability to hire |
| `FOLLOWUP_AFTER_DAYS` | `6` | Set `FOLLOWUP_ENABLED=false` to disable entirely |
| `GROQ_API_KEY` | none | Optional. Writes one tailored sentence per email |
| `GITHUB_TOKEN` | none | Optional. Raises the API limit from 60 to 5000 per hour |
| `DRY_RUN` | `true` | Nothing is sent until this is false |

Three files hold your own words:

* `config/template.txt` is the email. The first line must start with `Subject:`.
* `config/reply.txt` is optional. When it exists, every person who replies gets
  an answer drafted from it, threaded under their message and saved to your
  Drafts folder over IMAP. Nothing is sent; you review each draft and send it
  yourself. One draft per person, and out-of-office replies are skipped. Write
  links as `[label](url)` to make them clickable. Delete the file to turn
  drafting off. See `config/reply.example.txt`.
* `config/aspects.txt` lists what you have actually built, one short phrase per
  line. The generated sentence may only draw on this list, so it cannot invent
  experience you do not have. An empty file means that sentence is left out.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- SCHEDULING -->
## Scheduling

```sh
powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
```

Registers five Windows tasks:

| Task | When | What |
| --- | --- | --- |
| `mailbot-draft` | Every day, 06:00 to 23:00, every 2h | Render drafts, copy them to Gmail Drafts |
| `mailbot-prep` | Weekdays 08:25 | Full mailbox scan before the window opens |
| `mailbot-send` | Weekdays 08:30 to 13:30, every 15m | Send two messages per tick |
| `mailbot-build` | Every day 19:30 | Discover and enrich new companies |
| `mailbot-digest` | Fridays 17:00 | Email you a weekly summary |

The split that matters is drafting from sending. Drafting has no deadline, so
it runs whenever the machine happens to be on and stops as soon as
`QUEUE_TARGET` drafts are waiting. Sending is the only job that has to land in
a particular hour, and it now does nothing but send what is already written.

That ordering is the fix for a real failure. Drafting used to happen at 08:25
on weekdays, so a laptop asleep at 08:25 produced no drafts, and a laptop
asleep until 10:25 also missed every send tick before then. One closed lid
cost the whole day.

Enrichment stays separate for its own reason: it makes slow calls to third
parties, and a single hung request should never eat the send window.

Each tick only contacts people whose local window is currently open, so the
08:30 to 13:30 span covers Eastern recipients in their morning and then
Pacific ones in theirs. Ticks when nobody's window is open simply send
nothing.

### If the laptop is gone

With `MIRROR_TO_DRAFTS=true` every queued draft also sits in your Gmail Drafts
folder, personalised and carrying the resume. If the machine is closed all
day, open Gmail on a phone and send them by hand.

Nothing goes out twice. The bot deletes its copy the moment SMTP accepts a
message, and reads the Sent folder before every send run, so a draft you sent
yourself is recorded and dropped from the queue before the scheduled run
reaches it. An unfinished draft is never mirrored at all, because a copy in
Drafts is something a human can send without passing the checks `send` makes.

### Priority companies

`config/priority.txt` is a hand-written list of companies to put at the front.
They skip the team-size, age and location gates, and are enriched, drafted and
sent before anything else. Nothing about sending is relaxed: they still need a
verified contact and still obey the cap, the window and the suppression list.

```sh
python -m src.main priority --resolve
```

resolves a domain for any entry that has none and writes it back to the file.
It only accepts a guessed domain when the site serving it names the company,
and reports the rest for you to fill in by hand. Mailing a stranger who
happens to own the `.com` is worse than missing a company.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ROADMAP -->
## Roadmap

- [x] Free discovery, enrichment and SMTP verification
- [x] Per-domain address pattern learning
- [x] Recipient-local send windows
- [x] Reply, bounce and opt-out handling
- [x] Drafted answers to replies, left for you to send
- [x] Setup and progress console
- [x] Packaged Windows build
- [ ] macOS and Linux builds
- [ ] Funding signal keyed on domain rather than company name
- [ ] Headcount growth as a ranking input, once enough history accumulates
- [x] Discovery sources beyond Y Combinator
- [ ] City startup directories, for cities GitHub and YC both under-serve

See the [open issues](https://github.com/teddycitrus/mailbot/issues) for the
full list.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTRIBUTING -->
## Contributing

Contributions are welcome. Fork the repo, create a feature branch, and open a
pull request.

```sh
git checkout -b feature/your-feature
git commit -m "Add your feature"
git push origin feature/your-feature
```

Please run the tests first:

```sh
python -m pytest tests/ -q
```

If you add a discovery source, keep it free and keep it polite: honour
robots.txt, rate limit it, and cap the calls it can make in one run.

> **Disclaimer:** This tool sends unsolicited email. That is legal in most
> places when the message is genuine, identifies you, and offers a way to opt
> out, all of which it does by default. It is your responsibility to know the
> rules where you and your recipients live, including CAN-SPAM, CASL and GDPR.
> Do not raise the daily limit to a number a human could not plausibly write by
> hand, and do not remove the opt-out handling.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTACT -->
## Contact

Project Link: [https://github.com/teddycitrus/mailbot](https://github.com/teddycitrus/mailbot)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- MARKDOWN LINKS & BADGES -->
[contributors-shield]: https://img.shields.io/github/contributors/teddycitrus/mailbot.svg?style=for-the-badge
[contributors-url]: https://github.com/teddycitrus/mailbot/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/teddycitrus/mailbot.svg?style=for-the-badge
[forks-url]: https://github.com/teddycitrus/mailbot/network/members
[stars-shield]: https://img.shields.io/github/stars/teddycitrus/mailbot.svg?style=for-the-badge
[stars-url]: https://github.com/teddycitrus/mailbot/stargazers
[issues-shield]: https://img.shields.io/github/issues/teddycitrus/mailbot.svg?style=for-the-badge
[issues-url]: https://github.com/teddycitrus/mailbot/issues
[license-shield]: https://img.shields.io/github/license/teddycitrus/mailbot.svg?style=for-the-badge
[license-url]: https://github.com/teddycitrus/mailbot/blob/main/LICENSE
[release-shield]: https://img.shields.io/github/v/release/teddycitrus/mailbot.svg?style=for-the-badge
[release-url]: https://github.com/teddycitrus/mailbot/releases/latest
[downloads-shield]: https://img.shields.io/github/downloads/teddycitrus/mailbot/total.svg?style=for-the-badge
[downloads-url]: https://github.com/teddycitrus/mailbot/releases
