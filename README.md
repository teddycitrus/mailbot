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
sends a small number of them each morning. It then reads the replies and stops
contacting anyone who asks it to.

Commercial tools in this space charge for the same pipeline. The expensive part
of those products is not the technique, it is the proprietary contact corpus and
the pool of IP addresses used to verify against. Mailbot builds its own corpus
as it goes and stays deliberately slow, which costs nothing.

It runs entirely on your machine. There is no account, no server, and no
telemetry. Your credentials, your resume and your contact database never leave
the computer you run it on.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### How It Finds Addresses

Four free sources, in the order they are tried:

| Source | What it gives | Cost |
| --- | --- | --- |
| Y Combinator OSS directory | Companies, location, headcount, batch, hiring flag | No key |
| Company website crawl | Any address the company publishes itself | No key |
| Public git commits | Addresses engineers published in their own commits | Optional token |
| Hacker News "Who is hiring" | Addresses the hiring person typed themselves | No key |

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
* Your resume as a PDF.
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
python -m src.main discover       # find qualifying companies
python -m src.main hn             # import the Hacker News hiring thread
python -m src.main enrich         # find and verify a contact at each company
python -m src.main queue          # render personalised drafts
python -m src.main preview        # read the drafts before anything is sent
python -m src.main send           # send, respecting cap and window
python -m src.main followup       # one nudge to people who never replied
python -m src.main inbox          # scan for bounces, replies and opt-outs
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
| `TARGET_LOCATIONS` | `San Francisco,New York,Toronto` | Comma separated |
| `MAX_EMPLOYEES`, `MAX_COMPANY_AGE_YEARS` | `200`, `5` | Target filter |
| `PREFER_RECENTLY_FUNDED` | `true` | Rank by recency of funding blended with ability to hire |
| `FOLLOWUP_AFTER_DAYS` | `6` | Set `FOLLOWUP_ENABLED=false` to disable entirely |
| `GROQ_API_KEY` | none | Optional. Writes one tailored sentence per email |
| `GITHUB_TOKEN` | none | Optional. Raises the API limit from 60 to 5000 per hour |
| `DRY_RUN` | `true` | Nothing is sent until this is false |

Two files hold your own words:

* `config/template.txt` is the email. The first line must start with `Subject:`.
* `config/aspects.txt` lists what you have actually built, one short phrase per
  line. The generated sentence may only draw on this list, so it cannot invent
  experience you do not have. An empty file means that sentence is left out.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- SCHEDULING -->
## Scheduling

```sh
powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
```

Registers three Windows tasks:

| Task | When | What |
| --- | --- | --- |
| `mailbot-send` | Weekdays 08:35 and 11:35 | Inbox scan, queue, send, follow up |
| `mailbot-build` | Weekdays 19:30 | Discover and enrich new companies |
| `mailbot-digest` | Fridays 17:00 | Email you a weekly summary |

Sending and enrichment are separate on purpose. Enrichment makes slow calls to
third parties, and a single hung request should never eat the send window.

The two send times cover different timezones: 08:35 reaches recipients on
Eastern time in their morning, 11:35 reaches Pacific in theirs. Each run only
contacts people whose local window is currently open.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ROADMAP -->
## Roadmap

- [x] Free discovery, enrichment and SMTP verification
- [x] Per-domain address pattern learning
- [x] Recipient-local send windows
- [x] Reply, bounce and opt-out handling
- [x] Setup and progress console
- [x] Packaged Windows build
- [ ] macOS and Linux builds
- [ ] Funding signal keyed on domain rather than company name
- [ ] Headcount growth as a ranking input, once enough history accumulates
- [ ] Pluggable discovery sources beyond Y Combinator

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
