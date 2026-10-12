# MID Procurement Control

A dashboard for cards MID procurement across six acquiring rails: turnaround
per party, an acquirer scorecard, parsed bank replies with a review queue,
and a per-request timeline.

One self-contained `index.html`. No build step, no dependencies, no server.

## Deploy

Vercel will detect this as a static site. Nothing to configure.

```
npx vercel --prod
```

Or connect the folder as a repo in the Vercel dashboard.

## A note on the data

Every acquirer and merchant here is invented. The figures are measured
against a seeded synthetic dataset of 360 merchants, 1,380 requests and 653
bank emails, not live traffic. No real institution, merchant, rate card or
template appears anywhere.
