# Tone and structure references for first messages (English, `language: en`)

<!-- v2, written 2026-11-26 from the ONIX project docs (UAE clinic lane, website copy) and the Serbian approved
messages. These are DRAFTS until you approve them: edit freely, then the drafter uses them as tone references, never
as templates to copy. Niche subsections are OPTIONAL and match the run's niche name (created in `go`); `### generic`
is always the fallback. Market notes: UAE first touch is Instagram / LinkedIn / Email; WhatsApp only after a reply. -->

Rules the drafter enforces for English (rules_en.py):
- Greeting: "Hi" for DMs, "Hello" for Email and Call. Never "Dear Sir/Madam", "Hope you're well", "I hope this finds you well".
- 2-3 sentences for DMs, exactly one question, and it comes last.
- No "!", no emoji, no ratings or reviews, no links in the first message, no "free" (the audit offer describes the
  work instead of pricing it), no "AI", "automation", "SEO", "Lighthouse", "you don't have a website".
- Personal voice ("I'm Kristijan"), never "we at ONIX".
- Never mention client names, the patient-records platform or the UAE enterprise work.

## Instagram (DM)

### clinic | dental | dentist | aesthetic | dermatology | medspa | ordinacija | stomatolog
Hi, I came across your profile and really liked the before-and-after posts, you can see the care that goes into each case. I'm Kristijan, I build websites for clinics, so I'm curious how most of your new patients find you?

Hi, I came across your profile and saw you offer implants, whitening and veneers. I'm Kristijan, I build websites for clinics, so I'm curious whether patients find you on Google when they search for a dentist nearby, or mostly through referrals?

Hi, I came across your profile and like how you explain treatments in short videos. I'm Kristijan, I build websites for clinics, so I'm curious whether people ever ask about appointments in DMs and the conversation just goes quiet?

### accommodation | stays | holiday homes | apartments | hotel | villa | smeštaj
Hi, I came across your profile and the apartments look really clean and well put together. I'm Kristijan, I build websites for local businesses, so I'm curious whether guests book with you directly, or mostly through Booking and Airbnb?

### generic
Hi, I came across your profile and like the way you present your services. I'm Kristijan, I build websites for local businesses, so I'm curious how new clients usually find you?

Hi, I came across your profile and your photos of finished work look great, and you clearly have a strong following. I'm Kristijan, I build websites for local businesses, so I'm curious whether people find you on Google when they search for what you do, or mostly through referrals?

## Instagram (DM) with a friendly warning
<!-- Use when the drafter's SM Audit note shows a broken link in bio, a dead booking link or an outdated phone number.
State the fact plainly, no judgement, question still last. -->
Hi, I came across your profile and noticed the link in your bio opens an error page, so I thought you'd want to know. I'm Kristijan, I build websites for local businesses, so I'm curious whether most people book with you by phone or through messages?

## Instagram (DM) with audit offer
<!-- Used only when `outreach.first_offer: audit` is on, which stays OFF until website P0 proof (demo, sample audit,
About page, review page) is live. The offer replaces the question and stays a question. -->
Hi, I came across your profile and really liked the before-and-after posts. I'm Kristijan, I build websites for clinics. If it helps, I can look at how patients get from Google to a booked appointment on your site and send you a one-page note with concrete suggestions, would that be useful?

Hi, I came across your profile and like the way you present your services. I'm Kristijan, I build websites for local businesses. I can check how people on a phone get from your site to a booking and send a one-page note with concrete suggestions, would that be helpful?

## Facebook
<!-- Adds the "pass it to the owner" line, same as the Serbian Facebook section. -->

### clinic | dental | dentist | aesthetic | dermatology | medspa | ordinacija | stomatolog
Hi, I came across your page and see you regularly share tips on dental care. I'm Kristijan, I build websites for clinics, so I'm curious how most of your new patients find you? If someone from the team reads this, please pass it on to the owner.

### generic
Hi, I came across your page and like how you present your work. I'm Kristijan, I build websites for local businesses, so I'm curious how new clients usually reach you? If someone from the team reads this, please pass it on to the owner.

## LinkedIn
<!-- UAE lane: owner or practice manager. Connection note has a 300-character limit, so one or two sentences and no
pitch. The message after acceptance follows the DM structure. -->

### connection note
Hi {first_name}, I came across {clinic_name} while looking at clinics in {city}. I build websites for clinics and would be glad to connect.

### after accept | clinic | dental | dentist | aesthetic | dermatology | medspa
Hi {first_name}, thanks for connecting. I'm Kristijan, I build websites for clinics, and I noticed {clinic_name} offers {specific treatment}. I'm curious how most new patients find you today, through referrals, Google or social media?

### after accept | generic
Hi {first_name}, thanks for connecting. I'm Kristijan, I build websites for local businesses, and I like how {business_name} presents its work. I'm curious how most new clients find you today?

## Email
<!-- Format from the drafter: Subject line, blank line, 70-110 words, opt-out line, signature. UAE: email is
follow-up only, so these are used when Email is the top reachable channel in a market that allows it as first touch. -->

### clinic | dental | dentist | aesthetic | dermatology | medspa | ordinacija | stomatolog
Subject: Quick question about appointment booking

Hello, this is Kristijan, I build websites for clinics. I came across {clinic_name} and liked how clearly you present your treatments and team. I'm curious how most of your new patients find you today: referrals, Google or social media? I ask because many clinics I look at still take bookings by phone only, and I'd like to understand how it works for you.

If this isn't relevant, just let me know and I won't write again.

Kristijan, ONIX Web Lab, onixweblab.com

### generic
Subject: Quick question about new clients

Hello, this is Kristijan, I build websites for local businesses. I came across {business_name} and like the way you present your services. I'm curious how new clients usually find you: referrals, Google or social media?

If this isn't relevant, just let me know and I won't write again.

Kristijan, ONIX Web Lab, onixweblab.com

## WhatsApp (after a reply only)
<!-- Never first touch in the UAE (`first_touch_excluded`). Used when the lead replied on another channel and asked
to continue on WhatsApp, or gave a number. Here a link and the offer are allowed. -->
Hi, it's Kristijan from ONIX Web Lab, we spoke on Instagram. As promised, here's the short example: {link}. Does it look close to what you had in mind?

Hi, it's Kristijan, following up on our LinkedIn chat. I've put together the one-page note for {clinic_name}, shall I send it here or by email?

## Call
Hello, this is Kristijan, I build websites for local businesses. I came across {business_name}, do you have a minute for one quick question about how new clients find you?

Hello, this is Kristijan, I build websites for clinics. I came across {clinic_name}, do you have a minute for one quick question about how patients book appointments with you?

## Questions by business type
<!-- If the run's niche (created in `go`) has its own question, the drafter uses that first. -->
Pick one, adapt it to the business, never copy one trade's wording to another.
- Open: "how do new clients usually find you?" (clinics: "patients")
- Google (strongest for services people search for): "do people find you on Google when they search for {specific service}, or mostly through referrals?"
- Messages and bookings: "do people ever ask about prices or appointments in DMs and the conversation just goes quiet?"
- After hours: "what happens when someone calls or messages after you've closed?"
- Already has a site: "is your website bringing in new clients right now, or do most come through referrals?"
- Accommodation: "do guests book with you directly, or mostly through Booking and Airbnb?"
- Specific angle from the bio or the SM Audit note: a named treatment, home visits, corporate clients.
