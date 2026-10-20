/**
 * Who makes the software and where its public pages live. This is the ONE place that names the maker
 * and the website: every dialog, link and share caption imports from here, so a change of address or
 * company name is a one-line edit. (A unit test, brand.test.mjs, fails if a source file links to
 * a code-hosting site or names a person.)
 *
 * The update address and the "download page" the Rust shell offers live in src-tauri/src/updates.rs
 * and src-tauri/tauri.conf.json; docs/UPDATES.md lists all the places.
 */

/** The public website (no trailing slash). */
export const WEBSITE_URL = 'https://radarx.labshope.com';

/** The legal entity that makes and publishes the software. */
export const COMPANY = 'LabShope Technologies';

/** The credit line shown in About and written where the software names its maker. */
export const PRODUCT_CREDIT = `Radarx Desktop by ${COMPANY}`;

/** Where installers are downloaded (the website's download section). */
export const DOWNLOAD_PAGE_URL = `${WEBSITE_URL}/#download`;

/** Terms (licence) and privacy notice, as pages of the website. */
export const TERMS_URL = `${WEBSITE_URL}/terms.html`;
export const PRIVACY_URL = `${WEBSITE_URL}/privacy.html`;

/** Documentation of the open xradar library the engine is built on (third party, not ours). */
export const XRADAR_DOCS_URL = 'https://docs.openradarscience.org/projects/xradar/';
