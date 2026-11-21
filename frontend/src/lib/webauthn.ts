/** Thin wrapper around the browser's native WebAuthn API
 * (`navigator.credentials`) — no client library, just the base64url
 * conversions the server's JSON options/response shapes need on either
 * side of the raw `ArrayBuffer`s the browser API itself deals in. */

interface JsonCredentialDescriptor {
  id: string;
  type: string;
  transports?: string[];
}

interface RegistrationOptionsJSON {
  challenge: string;
  rp: { id?: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: string; alg: number }[];
  timeout?: number;
  excludeCredentials?: JsonCredentialDescriptor[];
  authenticatorSelection?: unknown;
  attestation?: string;
}

interface RequestOptionsJSON {
  challenge: string;
  timeout?: number;
  rpId?: string;
  allowCredentials?: JsonCredentialDescriptor[];
  userVerification?: string;
}

function base64urlToBuffer(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function toDescriptors(list: JsonCredentialDescriptor[] | undefined): PublicKeyCredentialDescriptor[] {
  return (list ?? []).map((c) => ({
    id: base64urlToBuffer(c.id),
    type: "public-key",
    transports: c.transports as AuthenticatorTransport[] | undefined,
  }));
}

export function isWebauthnSupported(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential && !!navigator.credentials;
}

export async function registerWebauthnCredential(optionsJSON: Record<string, unknown>): Promise<unknown> {
  const json = optionsJSON as unknown as RegistrationOptionsJSON;

  const options: PublicKeyCredentialCreationOptions = {
    rp: json.rp,
    user: { id: base64urlToBuffer(json.user.id), name: json.user.name, displayName: json.user.displayName },
    challenge: base64urlToBuffer(json.challenge),
    pubKeyCredParams: json.pubKeyCredParams as PublicKeyCredentialParameters[],
    timeout: json.timeout,
    excludeCredentials: toDescriptors(json.excludeCredentials),
    authenticatorSelection: json.authenticatorSelection as AuthenticatorSelectionCriteria | undefined,
    attestation: json.attestation as AttestationConveyancePreference | undefined,
  };

  const credential = (await navigator.credentials.create({ publicKey: options })) as PublicKeyCredential;
  const response = credential.response as AuthenticatorAttestationResponse;

  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      attestationObject: bufferToBase64url(response.attestationObject),
    },
  };
}

export async function getWebauthnAssertion(optionsJSON: Record<string, unknown>): Promise<unknown> {
  const json = optionsJSON as unknown as RequestOptionsJSON;

  const options: PublicKeyCredentialRequestOptions = {
    challenge: base64urlToBuffer(json.challenge),
    timeout: json.timeout,
    rpId: json.rpId,
    allowCredentials: toDescriptors(json.allowCredentials),
    userVerification: json.userVerification as UserVerificationRequirement | undefined,
  };

  const credential = (await navigator.credentials.get({ publicKey: options })) as PublicKeyCredential;
  const response = credential.response as AuthenticatorAssertionResponse;

  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      authenticatorData: bufferToBase64url(response.authenticatorData),
      signature: bufferToBase64url(response.signature),
      userHandle: response.userHandle ? bufferToBase64url(response.userHandle) : null,
    },
  };
}
