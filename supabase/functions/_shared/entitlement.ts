// Entitlement tokens: the one-hour sign-in that a personal access token is exchanged for.
//
// An ES256 JWT signed with a private key that exists only as the Edge Function secret
// ENTITLEMENT_PRIVATE_KEY (PKCS8 PEM). It is NOT a Supabase token: a different issuer, a different
// audience and a different key, so row-level security never accepts it and the engine verifies it
// against its own key list (python/engine/account/tokens.py). Plain WebCrypto, no dependencies, and
// only syntax Node can run by stripping types, so the tests (entitlement.test.mjs) need no Deno.

export const ISSUER = 'radarx-desktop/entitlement/v1';
export const AUDIENCE = 'radarx-cli';
export const LIFETIME_SECONDS = 3600;

const ECDSA = { name: 'ECDSA', namedCurve: 'P-256' } as const;
const encoder = new TextEncoder();

export class EntitlementError extends Error {
  reason: string;
  constructor(reason: string) {
    super(reason);
    this.reason = reason;
  }
}

export function b64urlEncode(bytes: Uint8Array): string {
  let text = '';
  for (const b of bytes) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new EntitlementError('format');
  const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(text)));
  return Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** The DER bytes of a PKCS8 PEM ("-----BEGIN PRIVATE KEY-----"). Nothing else is accepted. */
export function pemToPkcs8(pem: string): Uint8Array {
  const match = /^-----BEGIN PRIVATE KEY-----([A-Za-z0-9+/=\s]+)-----END PRIVATE KEY-----$/.exec(
    (pem ?? '').trim(),
  );
  if (!match) throw new EntitlementError('ENTITLEMENT_PRIVATE_KEY must be a PKCS8 PEM (BEGIN PRIVATE KEY)');
  return Uint8Array.from(atob(match[1].replace(/\s+/g, '')), (c) => c.charCodeAt(0));
}

export interface SigningKey {
  privateKey: CryptoKey;
  publicKey: CryptoKey;
  publicJwk: { kty: 'EC'; crv: 'P-256'; x: string; y: string };
  /** RFC 7638 thumbprint of the public key: the same value the key script prints. */
  kid: string;
}

/** RFC 7638 JWK thumbprint (members in lexicographic order, no whitespace). */
export async function thumbprint(jwk: { crv: string; kty: string; x: string; y: string }): Promise<string> {
  const canonical = `{"crv":"${jwk.crv}","kty":"${jwk.kty}","x":"${jwk.x}","y":"${jwk.y}"}`;
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(canonical)));
  return b64urlEncode(digest);
}

export async function loadSigningKey(pem: string): Promise<SigningKey> {
  const der = pemToPkcs8(pem);
  let exportable: CryptoKey;
  try {
    exportable = await crypto.subtle.importKey('pkcs8', der, ECDSA, true, ['sign']);
  } catch {
    throw new EntitlementError('ENTITLEMENT_PRIVATE_KEY is not a P-256 key');
  }
  const jwk = await crypto.subtle.exportKey('jwk', exportable);
  if (jwk.kty !== 'EC' || jwk.crv !== 'P-256' || !jwk.x || !jwk.y) {
    throw new EntitlementError('ENTITLEMENT_PRIVATE_KEY is not a P-256 key');
  }
  const publicJwk = { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y } as const;
  // the signing key itself is imported again as non-extractable
  const privateKey = await crypto.subtle.importKey('pkcs8', der, ECDSA, false, ['sign']);
  const publicKey = await crypto.subtle.importKey('jwk', publicJwk, ECDSA, false, ['verify']);
  return { privateKey, publicKey, publicJwk, kid: await thumbprint(publicJwk) };
}

/**
 * The verifying key from the PUBLIC JWK secret (ENTITLEMENT_PUBLIC_JWK): what log-activity needs, so
 * that function never has to read the private key. A JWK that carries a private part ("d") is refused:
 * the wrong secret was pasted, and the mistake should be loud.
 */
export async function loadVerifyKey(jwkText: string): Promise<Pick<SigningKey, 'publicKey' | 'publicJwk' | 'kid'>> {
  let jwk: Record<string, unknown>;
  try {
    jwk = JSON.parse(jwkText);
  } catch {
    throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK is not JSON');
  }
  if (jwk === null || typeof jwk !== 'object') throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK is not a JWK');
  if ('d' in jwk) throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK contains a private key: set the PUBLIC half only');
  if (jwk.kty !== 'EC' || jwk.crv !== 'P-256' || typeof jwk.x !== 'string' || typeof jwk.y !== 'string') {
    throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK is not a P-256 public key');
  }
  const publicJwk = { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y } as const;
  let publicKey: CryptoKey;
  try {
    publicKey = await crypto.subtle.importKey('jwk', publicJwk, ECDSA, false, ['verify']);
  } catch {
    throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK is not a valid P-256 public key');
  }
  const kid = await thumbprint(publicJwk);
  if (typeof jwk.kid === 'string' && jwk.kid !== kid) throw new EntitlementError('ENTITLEMENT_PUBLIC_JWK has a kid that is not its thumbprint');
  return { publicKey, publicJwk, kid };
}

export async function signEntitlement(claims: Record<string, unknown>, key: SigningKey): Promise<string> {
  const header = { alg: 'ES256', typ: 'JWT', kid: key.kid };
  const input =
    b64urlEncode(encoder.encode(JSON.stringify(header))) + '.' + b64urlEncode(encoder.encode(JSON.stringify(claims)));
  // WebCrypto's ECDSA signature is already the fixed-width r||s form that JWS wants
  const signature = new Uint8Array(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key.privateKey, encoder.encode(input)));
  return input + '.' + b64urlEncode(signature);
}

/** Verify a token this module signed. Throws EntitlementError(reason). Used by log-activity. */
export async function verifyEntitlement(
  token: string,
  key: Pick<SigningKey, 'publicKey' | 'kid'>,
  nowSeconds: number,
): Promise<Record<string, unknown>> {
  if (typeof token !== 'string' || token.length > 4096) throw new EntitlementError('format');
  const parts = token.split('.');
  if (parts.length !== 3) throw new EntitlementError('format');
  let header: Record<string, unknown>;
  let claims: Record<string, unknown>;
  try {
    header = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[1])));
  } catch {
    throw new EntitlementError('format');
  }
  // the algorithm and key are fixed here, never taken from the token
  if (header.alg !== 'ES256' || header.kid !== key.kid) throw new EntitlementError('header');
  const signature = b64urlDecode(parts[2]);
  if (signature.length !== 64) throw new EntitlementError('signature');
  const good = await crypto.subtle.verify(
    { name: 'ECDSA', hash: 'SHA-256' },
    key.publicKey,
    signature,
    encoder.encode(parts[0] + '.' + parts[1]),
  );
  if (!good) throw new EntitlementError('signature');
  if (claims.iss !== ISSUER || claims.aud !== AUDIENCE || claims.src !== 'pat') throw new EntitlementError('claims');
  const { exp, iat, sub, jti } = claims;
  if (typeof sub !== 'string' || !/^[0-9a-f-]{36}$/i.test(sub) || typeof jti !== 'string' || !jti) {
    throw new EntitlementError('claims');
  }
  if (typeof exp !== 'number' || typeof iat !== 'number') throw new EntitlementError('claims');
  if (exp <= nowSeconds) throw new EntitlementError('expired');
  if (iat > nowSeconds + 60 || exp - iat > LIFETIME_SECONDS) throw new EntitlementError('claims');
  return claims;
}
