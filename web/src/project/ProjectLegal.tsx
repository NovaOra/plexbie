import { Link } from "react-router-dom";
import { LegalDoc, useTitle } from "../components/ui";
import { CONTACT, OPERATOR } from "./links";

// The project site's own policies. This site only describes Plexbie: nobody signs in
// here. Each household's Plexbie has its own Privacy and Terms, written for whoever
// runs it. Keep every statement true to what this site does.

const UPDATED = "October 4, 2026";

const Mail = () => <a href={`mailto:${CONTACT}`}>{CONTACT}</a>;

export function ProjectPrivacy() {
  useTitle("Privacy");
  return (
    <LegalDoc title="Privacy" updated={UPDATED}>
      <p>
        plexbie.com is the website of Plexbie, a free, open-source project by {OPERATOR}. It describes the software and
        where to get it. Questions go to <Mail />.
      </p>
      <h2>What this site collects</h2>
      <ul>
        <li>Nothing you type, because there’s nothing to sign in to or fill in here.</li>
        <li>
          The site sets no cookies of its own, stores nothing on your device, and has no advertising and no third-party
          scripts or embeds. Cloudflare, which serves it, may set one short-lived security cookie (__cf_bm) to tell people
          from bots; it isn’t used for counting.
        </li>
        <li>
          <b>Visit counts.</b> The site counts its own visits, so we can see what people find useful: which pages are
          opened, the website that sent you (or a campaign tag in the link), your country and region, your kind of device,
          browser and system, which buttons, links and TV channels are used, how much of the tour video is watched, and
          how long a page is on screen and how far down it’s read.
        </li>
        <li>
          <b>The demo too.</b> The demo at demo.plexbie.com is counted the same way, with the same limits below. It’s a
          pretend household: nothing you do there is sent anywhere else or kept, apart from these counts.
        </li>
        <li>
          <b>No IP address is kept.</b> To count each visitor once a day, your IP address and browser are combined with
          the date and a secret, and turned into a number that can’t be turned back and changes every day. The address
          itself is never stored, and you can’t be followed from one day to the next.
        </li>
        <li>
          If your browser sends Global Privacy Control or Do Not Track, none of this is counted. Counts are kept about
          13 months, and only the project’s maintainer can see them.
        </li>
        <li>
          Cloudflare serves this site and its videos and protects it from abuse. It keeps basic request logs, and its
          privacy policy applies to that.
        </li>
        <li>
          <b>Nothing is sold or shared.</b> The counts aren’t sold, shared with anyone or used for advertising, and
          nothing here is combined with information from anywhere else.
        </li>
        <li>This site isn’t aimed at children, and we don’t knowingly collect anything about them.</li>
      </ul>
      <h2>Why we count, and on what basis</h2>
      <p>
        Counting visits tells us which parts of the site and of Plexbie people find useful, and where they hear about
        it. Where the law asks for a reason (such as the GDPR in Europe and the UK), it’s our legitimate interest in
        understanding how the site is used, kept to the minimum above so it doesn’t outweigh your privacy.
      </p>
      <h2>Where it’s kept</h2>
      <p>
        The counts are stored with Cloudflare, which handles them for us and only as we ask. Cloudflare runs worldwide,
        so they may be processed outside your country, under Cloudflare’s own data protection commitments.
      </p>
      <h2>Your choices and rights</h2>
      <ul>
        <li>
          <b>To not be counted at all,</b> turn on Global Privacy Control or Do Not Track in your browser. We honour
          both, everywhere on this site and the demo.
        </li>
        <li>
          Depending on where you live, you may have the right to ask what we hold about you, to have it corrected or
          deleted, and to object to it being used. Write to <Mail /> and we’ll answer within a month. Because no IP
          address or name is kept and the daily visitor number changes every day, we usually can’t tell which counts
          are yours; if you can help us find them (the day and pages you visited, for example), we will.
        </li>
        <li>
          If you’re in the EU or the UK and think we’ve got this wrong, you can also complain to your data protection
          authority.
        </li>
      </ul>
      <h2>A household’s Plexbie is separate</h2>
      <p>
        Plexbie runs on each household’s own server, at its own address. What one handles, and who to ask about it, is on
        that site’s own Privacy page. The project can’t see into anyone’s Plexbie.
      </p>
      <h2>Changes</h2>
      <p>If this page changes, the date at the top changes too.</p>
    </LegalDoc>
  );
}

export function ProjectTerms() {
  useTitle("Terms");
  return (
    <LegalDoc title="Terms of use" updated={UPDATED}>
      <p>These terms cover plexbie.com, run by {OPERATOR}. Questions go to <Mail />.</p>
      <h2>The software</h2>
      <p>
        Plexbie is free and open source under the GNU Affero General Public License, version 3 or later. That licence,
        not this page, says what you may do with the code.
      </p>
      <h2>No guarantees</h2>
      <p>
        Plexbie and this site are made in spare time and provided as they are, without warranty. As far as the law allows,
        we’re not liable for any loss from using them.
      </p>
      <h2>Names and logos</h2>
      <p>
        The Plexbie name and logo belong to {OPERATOR}. Plex, Discord, TMDB and the other services Plexbie works with
        belong to their owners, who don’t endorse Plexbie.
      </p>
      <h2>Changes</h2>
      <p>If these terms change, the date at the top changes too. See also the <Link to="/privacy">privacy policy</Link>.</p>
    </LegalDoc>
  );
}
