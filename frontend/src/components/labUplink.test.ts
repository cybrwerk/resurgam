import { describe, expect, it } from "vitest";

import { ipv4ToNumber, splitDns, type UplinkFormState, uplinkPayload, validateUplink } from "./labUplink";

const STATIC: UplinkFormState = {
  vlanTag: "",
  ipMode: "static_range",
  rangeStart: "192.168.40.50",
  rangeEnd: "192.168.40.60",
  prefixLength: "24",
  gateway: "192.168.40.1",
  dnsServers: "192.168.40.2",
};

describe("labUplink", () => {
  it("parses IPv4 and DNS lists", () => {
    expect(ipv4ToNumber("10.0.0.1")).toBe(167772161);
    expect(ipv4ToNumber("256.0.0.1")).toBeNull();
    expect(ipv4ToNumber("10.0.0")).toBeNull();
    expect(splitDns(" 1.1.1.1, 9.9.9.9;8.8.8.8 ")).toEqual(["1.1.1.1", "9.9.9.9", "8.8.8.8"]);
  });

  it("accepts DHCP with or without a tag and a consistent static range", () => {
    expect(validateUplink({ ...STATIC, ipMode: "dhcp", rangeStart: "" })).toEqual({});
    expect(validateUplink({ ...STATIC, vlanTag: "4094" })).toEqual({});
  });

  it.each([
    [{ vlanTag: "0" }, "vlanTag", "vlanRange"],
    [{ vlanTag: "4095" }, "vlanTag", "vlanRange"],
    [{ rangeStart: "" }, "rangeStart", "required"],
    [{ rangeStart: "abc" }, "rangeStart", "notIpv4"],
    [{ rangeEnd: "192.168.41.10" }, "rangeEnd", "notSameSubnet"],
    [{ rangeStart: "192.168.40.70" }, "rangeEnd", "endBeforeStart"],
    [{ rangeStart: "192.168.40.0" }, "rangeStart", "networkOrBroadcast"],
    [{ prefixLength: "31" }, "prefixLength", "prefixRange"],
    [{ gateway: "10.0.0.1" }, "gateway", "gatewayOutsideSubnet"],
    [{ gateway: "192.168.40.55" }, "gateway", "gatewayInRange"],
    [{ gateway: "192.168.40.255" }, "gateway", "networkOrBroadcast"],
    [{ dnsServers: "" }, "dnsServers", "dnsRequired"],
    [{ dnsServers: "1.1.1.1,2.2.2.2,3.3.3.3,4.4.4.4" }, "dnsServers", "dnsTooMany"],
    [{ dnsServers: "dns.example" }, "dnsServers", "notIpv4"],
  ] as const)("flags %o as %s=%s", (patch, field, code) => {
    expect(validateUplink({ ...STATIC, ...patch })[field]).toBe(code);
  });

  it("builds the update payload and clears static fields for DHCP", () => {
    expect(uplinkPayload({ ...STATIC, vlanTag: "42", dnsServers: "1.1.1.1, 9.9.9.9" })).toEqual({
      lab_appliance_uplink_vlan_tag: 42,
      lab_appliance_uplink_ip_mode: "static_range",
      lab_appliance_uplink_ip_range_start: "192.168.40.50",
      lab_appliance_uplink_ip_range_end: "192.168.40.60",
      lab_appliance_uplink_ip_prefix_length: 24,
      lab_appliance_uplink_gateway: "192.168.40.1",
      lab_appliance_uplink_dns_servers: ["1.1.1.1", "9.9.9.9"],
    });
    expect(uplinkPayload({ ...STATIC, ipMode: "dhcp" })).toMatchObject({
      lab_appliance_uplink_vlan_tag: null,
      lab_appliance_uplink_ip_mode: "dhcp",
      lab_appliance_uplink_ip_range_start: null,
      lab_appliance_uplink_dns_servers: [],
    });
  });
});
