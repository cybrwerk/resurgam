import type { LabUplinkFields, LabUplinkIpMode, PveClusterOut } from "../api/types";

/** The probe appliance uplink settings as edited in the wizard's network step. */
export interface UplinkFormState {
  vlanTag: string;
  ipMode: LabUplinkIpMode;
  rangeStart: string;
  rangeEnd: string;
  prefixLength: string;
  gateway: string;
  dnsServers: string;
}

export type UplinkField = "vlanTag" | "rangeStart" | "rangeEnd" | "prefixLength" | "gateway" | "dnsServers";

/** Translation keys (labSetup.uplink.errors.*) per field; mirrors the backend's
 * `lab_setup/uplink.validate_uplink_config`, which has the final word. */
export type UplinkErrors = Partial<Record<UplinkField, string>>;

export function uplinkFormFromCluster(cluster: PveClusterOut): UplinkFormState {
  return {
    vlanTag: cluster.lab_appliance_uplink_vlan_tag?.toString() ?? "",
    ipMode: cluster.lab_appliance_uplink_ip_mode ?? "dhcp",
    rangeStart: cluster.lab_appliance_uplink_ip_range_start ?? "",
    rangeEnd: cluster.lab_appliance_uplink_ip_range_end ?? "",
    prefixLength: cluster.lab_appliance_uplink_ip_prefix_length?.toString() ?? "24",
    gateway: cluster.lab_appliance_uplink_gateway ?? "",
    dnsServers: (cluster.lab_appliance_uplink_dns_servers ?? []).join(", "),
  };
}

/** IPv4 dotted quad -> 32-bit number, or null. */
export function ipv4ToNumber(value: string): number | null {
  const parts = value.trim().split(".");
  if (parts.length !== 4) return null;
  let result = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    result = result * 256 + octet;
  }
  return result;
}

export function splitDns(value: string): string[] {
  return value
    .split(/[\s,;]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function validateUplink(form: UplinkFormState): UplinkErrors {
  const errors: UplinkErrors = {};
  if (form.vlanTag.trim() !== "") {
    const tag = Number(form.vlanTag);
    if (!Number.isInteger(tag) || tag < 1 || tag > 4094) errors.vlanTag = "vlanRange";
  }
  if (form.ipMode !== "static_range") return errors;

  const start = ipv4ToNumber(form.rangeStart);
  const end = ipv4ToNumber(form.rangeEnd);
  const gateway = ipv4ToNumber(form.gateway);
  const prefix = Number(form.prefixLength);
  if (start === null) errors.rangeStart = form.rangeStart.trim() ? "notIpv4" : "required";
  if (end === null) errors.rangeEnd = form.rangeEnd.trim() ? "notIpv4" : "required";
  if (gateway === null) errors.gateway = form.gateway.trim() ? "notIpv4" : "required";
  if (!Number.isInteger(prefix) || prefix < 8 || prefix > 30) errors.prefixLength = "prefixRange";
  const dns = splitDns(form.dnsServers);
  if (dns.length === 0) errors.dnsServers = "dnsRequired";
  else if (dns.length > 3) errors.dnsServers = "dnsTooMany";
  else if (dns.some((entry) => ipv4ToNumber(entry) === null)) errors.dnsServers = "notIpv4";

  if (start === null || end === null || errors.prefixLength) return errors;
  const size = 2 ** (32 - prefix);
  const network = Math.floor(start / size) * size;
  const broadcast = network + size - 1;
  if (end < network || end > broadcast) errors.rangeEnd = "notSameSubnet";
  else if (start > end) errors.rangeEnd = "endBeforeStart";
  else {
    if (start === network || start === broadcast) errors.rangeStart = "networkOrBroadcast";
    if (end === network || end === broadcast) errors.rangeEnd = "networkOrBroadcast";
  }
  if (gateway !== null && !errors.rangeEnd) {
    if (gateway < network || gateway > broadcast) errors.gateway = "gatewayOutsideSubnet";
    else if (gateway === network || gateway === broadcast) errors.gateway = "networkOrBroadcast";
    else if (gateway >= start && gateway <= end) errors.gateway = "gatewayInRange";
  }
  return errors;
}

/** The PVE cluster update fields for these settings (static fields are
 * cleared again when switching back to DHCP). */
export function uplinkPayload(form: UplinkFormState): LabUplinkFields {
  const tag = form.vlanTag.trim() === "" ? null : Number(form.vlanTag);
  if (form.ipMode !== "static_range") {
    return {
      lab_appliance_uplink_vlan_tag: tag,
      lab_appliance_uplink_ip_mode: "dhcp",
      lab_appliance_uplink_ip_range_start: null,
      lab_appliance_uplink_ip_range_end: null,
      lab_appliance_uplink_ip_prefix_length: null,
      lab_appliance_uplink_gateway: null,
      lab_appliance_uplink_dns_servers: [],
    };
  }
  return {
    lab_appliance_uplink_vlan_tag: tag,
    lab_appliance_uplink_ip_mode: "static_range",
    lab_appliance_uplink_ip_range_start: form.rangeStart.trim(),
    lab_appliance_uplink_ip_range_end: form.rangeEnd.trim(),
    lab_appliance_uplink_ip_prefix_length: Number(form.prefixLength),
    lab_appliance_uplink_gateway: form.gateway.trim(),
    lab_appliance_uplink_dns_servers: splitDns(form.dnsServers),
  };
}
