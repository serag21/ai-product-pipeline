# Little Haunts Club: Halloween 2026 distribution

This is the local release handoff for the 17-illustration edition. It records
which assets are prepared, which marketplace steps depend on account access,
and what still requires the owner's review. The dashboard is the live source
of selection, approval, package, and channel status.

## Current release state

- The nine original human-approved pages and the chosen New 02 skeleton astronomer and New 03 zombie chef remain selected and approved.
- Five archived choices for original slots 2, 3, 5, 7, and 8 are restored into the 17-page release as **Needs review**. Restoring them does not approve them.
- The old 12-page product approval is retained in the approval history and does not approve this 17-page edition.
- Product selection and artwork approval are separate from publication. No channel is published by this workflow without an owner action.
- The future biweekly Task Scheduler job is registered disabled. It cannot start another book before the launch channels have verified public URLs and the owner explicitly enables it.

The production scripts do not upload, publish, or post to any marketplace or social account during package creation.

## Etsy digital download

### Ready files and proposal

The package contains a prepared Etsy title, description, 13 tags, a proposed
$4.99 USD price, four square 2400 x 2400 px listing previews, US Letter and A4
PDFs, and two ZIPs containing the 17 individual PNG pages. The four downloads
fit Etsy's limit of five files; each file is checked below 19 MiB to leave room
under Etsy's 20 MB per-file cap. The listing copy discloses AI-generated
illustrations based on original Little Haunts Club scene concepts, then
human-curated and prepared for print. Etsy's current guidance also requires
seller-prompted AI creations to be disclosed in the listing description.

Before upload, the dashboard verifies all selected pages are individually
approved and the current package QA passes. The API path asks the seller to
choose the current Etsy category/taxonomy node and enter the shop currency
price; the proposed USD price is prefilled at $4.99.

### Etsy API and account requirements

The supported API sequence is: create a draft with `type=download`, upload
listing images and up to five digital files, update the draft to `download`,
then verify its public URL after the owner explicitly publishes. The local
publisher stores API credentials and OAuth tokens using Windows DPAPI. It
persists a creation intent before the draft POST; if Etsy's response is
ambiguous, it will not issue another create request until the seller reconciles
the exact-title draft. Media uploads resume individually after recoverable
failures. A completed draft is not published automatically.

For a shop owner connecting only their own shop, Etsy's current developer portal
offers a Seller App path. App key approval, seller OAuth, and the registered
redirect URI still require the account owner. Etsy requires an exact registered
HTTPS callback URL. The local dashboard collects that callback URI and uses
PKCE/state, then accepts the returned code and state for exchange. This local
code has been tested with mocked OAuth responses; no Etsy account, app approval,
OAuth grant, live draft, fee quote, or public listing has been verified here.

The ready manual fallback is Etsy's own [new listing flow](https://www.etsy.com/your/shops/me/tools/listings/create). It keeps the same
page-approval gate, file checks, AI disclosure, and $4.99 proposal. Current
official fee information lists a $0.20 listing fee and a 6.5% transaction fee;
payment processing varies by country, and other taxes or charges may apply.
For a $4.99 sale, the transaction-fee component alone is about $0.32 before
processing and other charges. Confirm the fee shown for the seller's country.

Official requirements: [Etsy digital files](https://help.etsy.com/hc/en-us/articles/115015628347-How-to-Manage-Your-Digital-Listings), [Etsy API digital listing flow](https://developers.etsy.com/documentation/tutorials/listings/), [Etsy OAuth and HTTPS redirect URIs](https://developers.etsy.com/documentation/essentials/oauth2/), [Etsy app access choices](https://developers.etsy.com/), [Etsy fees](https://help.etsy.com/hc/en-us/articles/115014483627-What-are-the-Fees-and-Taxes-for-Selling-on-Etsy), and [Etsy AI disclosure policy](https://help.etsy.com/hc/en-us/articles/360024112614-What-Can-I-Sell-on-Etsy).

## Amazon KDP paperback

The local KDP review edition uses an 8.5 x 11 inch black-ink, white-paper,
no-bleed interior with 36 total pages: a useful title page, a short coloring
and printing guide, 17 illustrations on odd pages 3 through 35, and blank
reverse pages. It meets KDP's 24-page minimum and uses at least 0.375 inch
margins. The one-page full cover wrap includes 0.125 inch bleed, the
page-count-based white-paper spine width, no spine text (KDP permits spine text
only above 79 pages), and an empty barcode reserve.

The source drawings are 1536 x 1984 px, about 198 DPI at their chosen large
placement. The interior PDF resamples them to 2325 x 3000 px, 300 DPI. That
meets the submitted raster dimension check but does not create new source
detail; the owner must inspect the PDF and KDP Print Previewer before accepting
print quality. Local page count, trim, margins, embedded image dimensions,
blank backs, cover size, and review gates are checked. A live KDP Print
Previewer pass has not been performed.

The package includes title, subtitle, description, seven keyword suggestions,
category suggestions, an AI-content disclosure checklist, cover, and interior.
No price is guessed: use the current KDP print-cost calculator after selecting
marketplace, paper, and ink. Amazon's documented publishing flow is through
KDP Bookshelf and Print Previewer; no supported public manuscript/cover upload
API was identified. Upload and publication remain manual and owner-controlled.

Official requirements: [KDP paperback submission guidelines](https://kdp.amazon.com/en_US/help/topic/G201857950), [KDP cover sizing and spine rules](https://kdp.amazon.com/en_US/help/topic/G201953020), [KDP AI-generated content disclosure](https://kdp.amazon.com/en_US/help/topic/G200672390), [KDP Bookshelf](https://kdp.amazon.com/en_US/bookshelf), and [KDP print-cost calculator](https://kdp.amazon.com/en_US/royalty-calculator).

## Payhip digital storefront

Payhip is tracked separately from Etsy. Its public API currently documents
coupon and license-key operations, not product creation or file uploads, so the
supported route is to create the product in the seller dashboard. The Etsy
PDF/PNG download set and copyable title, description, and tags are reusable;
confirm Payhip's own file limits, pricing, tax setup, and storefront URL in the
seller account. Payhip currently lists a 5% Free Forever transaction fee, plus
Stripe/PayPal processing fees; no paid Payhip plan is assumed. No Payhip account
or product is configured or live-tested.

Open [Payhip product creation](https://payhip.com/products/new). Official API
scope: [Payhip Public API](https://help.payhip.com/article/347-public-api).

## Promotion assets

The local promotion kit contains four 1000 x 1500 Pinterest images, Pinterest
titles/descriptions with a destination field, three 1080 x 1920 portrait stills,
Instagram/TikTok/YouTube Shorts copy, and an optional 9-second silent MP4 made
from existing artwork if FFmpeg is present. Before a real product URL is
verified, the destination is a conspicuous placeholder. After Etsy or Payhip
URL verification in the dashboard, these assets are refreshed to include that
URL. None are uploaded or posted automatically.

Pinterest's recommended standard Pin dimensions are 1000 x 1500 px (2:3).
The dashboard links to each platform's posting workflow. Account connections,
posting, and verified post URLs remain owner actions. No channel is labelled
Published unless a public HTTPS URL on the matching platform returns a page that
can be verified.

Official Pinterest specs: [Pinterest product specifications](https://help.pinterest.com/en/business/article/pinterest-product-specs).

## Channel checklist and actual test status

| Channel | Status before owner actions | Implemented and tested locally | Still required |
| --- | --- | --- | --- |
| Etsy digital | Needs action | Mock-tested OAuth/PKCE, draft creation, resumable files/images, uncertain-create reconciliation, publish gate and public-URL verification. Package limits and listing copy are locally checked. | Create/approve a Seller App, register the exact HTTPS callback, authorize the shop, review all 17 pages/package, create the draft, inspect Etsy's fees and listing, then explicitly publish or use the manual fallback. |
| KDP paperback | Needs action | 36-page interior, full cover wrap, metadata, layout/page-count/margins/blank-back checks and QA report built from selected local art. | Approve all artwork; upload manually in Bookshelf; inspect KDP Print Previewer, enter AI disclosures, select metadata/price, and publish only with explicit owner confirmation. |
| Payhip | Not configured | Local files and copy are prepared. Current official API scope reviewed. | Create/sign in to seller account, upload product, set price/tax settings, and verify its public URL. |
| Pinterest | Needs action | Four correctly sized pin files and destination descriptions prepared. | Use a verified Etsy/Payhip URL, connect/sign in, post pins, and record/verify the resulting public URLs. |
| Instagram | Needs action | Portrait stills and caption prepared. | Sign in, add verified product link, publish, and record/verify the public post URL. |
| TikTok | Needs action | Portrait stills, optional silent short video, and caption prepared. | Sign in, add verified product link, publish, and record/verify the public post URL. |
| YouTube Shorts | Needs action | Portrait stills, optional silent MP4, and title/description prepared. | Sign in, add verified product link, upload/post, and record/verify the public video URL. |

No Etsy, KDP, Payhip, Pinterest, Instagram, TikTok, or YouTube account was
connected, and no live API request, upload, publication, or social post was made
as part of this work. Automated HTTP URL verification is implemented but awaits
public URLs to check.

## Recurring production

`configure_biweekly_production.ps1` registers the 14-day Windows task with
StartWhenAvailable, IgnoreNew overlap handling, retries after crashes, and a
cross-session file lock. The future target is not configured and the task stays
disabled. Each run independently checks for verified Published Etsy, KDP, and
Payhip URLs before starting; activation also requires the owner's exact
`ALL CHANNELS LIVE` confirmation phrase and a future product prompt manifest.
Do not enable it until the Halloween release has launched on those initial
channels. After setting a future product folder and run ID, the owner can enable
it with:

```powershell
.\configure_biweekly_production.ps1 -Product "products\future-product" -RunId "factory-main" -Enable -ConfirmPublished "ALL CHANNELS LIVE"
```

Official Payhip fee reference: [Billing and upgrading](https://help.payhip.com/article/102-billing-and-upgrading) and [seller payment fees](https://help.payhip.com/article/396-resolve-payment-issues).
