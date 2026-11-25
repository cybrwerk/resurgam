import { useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { apiTokensApi, zabbixApi } from "../api/resources";
import { CopyButton } from "../components/CopyButton";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { Wizard } from "../components/Wizard";
import styles from "./ZabbixMonitoringPage.module.css";

const DEFAULT_TOKEN_DAYS = 365;
const MANUAL_URL = "https://github.com/cybrwerk/resurgam/blob/main/docs/manual/index.md#zabbix-monitoring";

type TokenMode = "create" | "existing";

/** A value to type or paste into Zabbix, with a copy button. */
function CopyValue({ label, value, display }: { label: string; value: string; display?: string }) {
  const { t } = useTranslation();
  return (
    <div className={styles.copyRow}>
      <span className={styles.copyLabel}>{label}</span>
      <code className={styles.copyValue}>{display ?? value}</code>
      <CopyButton text={value} label={t("zabbix.copy")} />
    </div>
  );
}

/** Administration -> Zabbix monitoring: a step-by-step setup of the
 * "RESURGAM by HTTP" Zabbix template -- token, template, host, macros. It
 * changes nothing in Zabbix itself; it creates the API token (if wanted)
 * and hands over everything to enter there. */
export function ZabbixMonitoringPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [tokenMode, setTokenMode] = useState<TokenMode>("create");
  const [tokenName, setTokenName] = useState("Zabbix");
  const [cidrs, setCidrs] = useState("");
  const [days, setDays] = useState(String(DEFAULT_TOKEN_DAYS));
  const [token, setToken] = useState<string | null>(null);
  const [tokenCopied, setTokenCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setupQuery = useQuery({ queryKey: ["zabbix-setup"], queryFn: zabbixApi.setup });
  const grantableQuery = useQuery({ queryKey: ["api-tokens-grantable"], queryFn: apiTokensApi.grantable });
  const maxDays = grantableQuery.data?.max_lifetime_days ?? 548;
  const daysNumber = Number(days);
  const daysValid = Number.isInteger(daysNumber) && daysNumber >= 1 && daysNumber <= maxDays;
  const cidrList = cidrs
    .split(/[\s,]+/)
    .map((c) => c.trim())
    .filter(Boolean);

  const createToken = useMutation({
    mutationFn: () =>
      apiTokensApi.create({
        name: tokenName.trim(),
        expires_in_days: daysNumber,
        read_only: true,
        grants: [{ role: "platform_admin", scope_type: "global", scope_id: null }],
        allowed_cidrs: cidrList.length > 0 ? cidrList : null,
      }),
    onSuccess: (created) => {
      setError(null);
      setToken(created.token);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const download = (action: () => Promise<void>) => {
    setError(null);
    action().catch((err) => setError(err instanceof ApiError ? err.message : t("common.loadError")));
  };

  const steps = [
    { id: "overview", title: t("zabbix.stepOverview") },
    { id: "token", title: t("zabbix.stepToken") },
    { id: "template", title: t("zabbix.stepTemplate") },
    { id: "host", title: t("zabbix.stepHost") },
    { id: "done", title: t("zabbix.stepDone") },
  ];
  const canAdvance =
    step === 0 ? setupQuery.isSuccess : step === 1 ? tokenMode === "existing" || (token !== null && tokenCopied) : true;

  const submitToken = (event: FormEvent) => {
    event.preventDefault();
    if (tokenName.trim() !== "" && daysValid && !createToken.isPending) createToken.mutate();
  };

  return (
    <div>
      <PageHeader title={t("nav.administrationZabbix")} description={t("zabbix.description")} />
      <Panel>
        <QueryState query={setupQuery}>
          {(setup) => (
            <Wizard
              steps={steps}
              currentStep={step}
              onStepClick={setStep}
              canAdvance={canAdvance}
              onBack={() => setStep((s) => Math.max(0, s - 1))}
              onNext={() => (step === steps.length - 1 ? navigate("/") : setStep((s) => s + 1))}
              nextLabel={t("zabbix.finish")}
            >
              {error && <InlineAlert tone="danger">{error}</InlineAlert>}

              {step === 0 && (
                <div className={styles.stepBody} data-testid="zabbix-step-overview">
                  <p>{t("zabbix.overviewIntro", { template: setup.template_name })}</p>
                  <ul className={styles.list}>
                    <li>{t("zabbix.monitorsApi")}</li>
                    <li>{t("zabbix.monitorsDatastores")}</li>
                    <li>{t("zabbix.monitorsJobs")}</li>
                  </ul>
                  <h3 className={styles.subTitle}>{t("zabbix.discoveredNow")}</h3>
                  <ul className={styles.list} data-testid="zabbix-discovered">
                    <li>
                      {t("zabbix.datastoresFound", { count: setup.datastores })}
                      {setup.datastores > setup.datastores_with_usage &&
                        ` ${t("zabbix.datastoresWithoutUsage", { count: setup.datastores - setup.datastores_with_usage })}`}
                    </li>
                    <li>{t("zabbix.jobTypesFound", { count: setup.job_types })}</li>
                  </ul>
                  {setup.datastores === 0 && <InlineAlert tone="info">{t("zabbix.nothingYet")}</InlineAlert>}
                  <h3 className={styles.subTitle}>{t("zabbix.requirements")}</h3>
                  <ul className={styles.list}>
                    <li>{t("zabbix.requireVersion", { version: setup.min_zabbix_version })}</li>
                    <li>
                      {t("zabbix.requireReach")} <code>{setup.appliance_url}</code>
                    </li>
                  </ul>
                  <InlineAlert tone="info">{t("zabbix.addressNote", { url: setup.appliance_url })}</InlineAlert>
                </div>
              )}

              {step === 1 && (
                <div className={styles.stepBody} data-testid="zabbix-step-token">
                  <p>{t("zabbix.tokenIntro")}</p>
                  <div className={styles.choice}>
                    <label>
                      <input
                        type="radio"
                        name="zabbix-token-mode"
                        checked={tokenMode === "create"}
                        onChange={() => setTokenMode("create")}
                      />{" "}
                      {t("zabbix.tokenCreate")}
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="zabbix-token-mode"
                        checked={tokenMode === "existing"}
                        onChange={() => setTokenMode("existing")}
                      />{" "}
                      {t("zabbix.tokenExisting")}
                    </label>
                  </div>
                  {tokenMode === "existing" && <p className="mutedText">{t("zabbix.tokenExistingHint")}</p>}
                  {tokenMode === "create" && token === null && (
                    <form onSubmit={submitToken} className={styles.form}>
                      <div className="field">
                        <label className="label" htmlFor="zabbix-token-name">
                          {t("zabbix.tokenName")}
                        </label>
                        <input
                          id="zabbix-token-name"
                          className="input"
                          value={tokenName}
                          maxLength={100}
                          onChange={(e) => setTokenName(e.target.value)}
                        />
                      </div>
                      <div className="field">
                        <label className="label" htmlFor="zabbix-token-cidrs">
                          {t("zabbix.tokenCidrs")}
                        </label>
                        <input
                          id="zabbix-token-cidrs"
                          className="input"
                          value={cidrs}
                          placeholder="192.0.2.10"
                          onChange={(e) => setCidrs(e.target.value)}
                        />
                        <span className="mutedText">{t("zabbix.tokenCidrsHint")}</span>
                      </div>
                      <div className="field">
                        <label className="label" htmlFor="zabbix-token-days">
                          {t("zabbix.tokenDays")}
                        </label>
                        <input
                          id="zabbix-token-days"
                          className="input"
                          type="number"
                          min={1}
                          max={maxDays}
                          value={days}
                          onChange={(e) => setDays(e.target.value)}
                        />
                        <span className="mutedText">{t("zabbix.tokenDaysHint", { max: maxDays })}</span>
                      </div>
                      <p className="mutedText">{t("zabbix.tokenScope")}</p>
                      <div>
                        <button
                          type="submit"
                          className="button buttonPrimary"
                          disabled={tokenName.trim() === "" || !daysValid || createToken.isPending}
                        >
                          {t("zabbix.tokenCreateButton")}
                        </button>
                      </div>
                    </form>
                  )}
                  {tokenMode === "create" && token !== null && (
                    <div className={styles.form} data-testid="zabbix-token-created">
                      <InlineAlert tone="info">{t("zabbix.tokenOnce")}</InlineAlert>
                      <CopyValue label="{$RESURGAM.TOKEN}" value={token} />
                      <label>
                        <input type="checkbox" checked={tokenCopied} onChange={(e) => setTokenCopied(e.target.checked)} />{" "}
                        {t("zabbix.tokenCopied")}
                      </label>
                    </div>
                  )}
                </div>
              )}

              {step === 2 && (
                <div className={styles.stepBody} data-testid="zabbix-step-template">
                  <p>{t("zabbix.templateIntro", { template: setup.template_name, version: setup.template_version })}</p>
                  <div>
                    <button type="button" className="button buttonPrimary" onClick={() => download(zabbixApi.downloadTemplate)}>
                      {t("zabbix.downloadTemplate")}
                    </button>
                  </div>
                  <ol className={styles.list}>
                    <li>{t("zabbix.importStep1")}</li>
                    <li>{t("zabbix.importStep2")}</li>
                    <li>{t("zabbix.importStep3")}</li>
                  </ol>
                  <p className="mutedText">{t("zabbix.templateUpdate")}</p>
                </div>
              )}

              {step === 3 && (
                <div className={styles.stepBody} data-testid="zabbix-step-host">
                  <p>{t("zabbix.hostIntro")}</p>
                  <ol className={styles.list}>
                    <li>{t("zabbix.hostStep1")}</li>
                    <li>{t("zabbix.hostStep2", { template: setup.template_name })}</li>
                    <li>{t("zabbix.hostStep3")}</li>
                  </ol>
                  <CopyValue label="{$RESURGAM.URL}" value={setup.appliance_url} />
                  {token !== null ? (
                    <CopyValue label="{$RESURGAM.TOKEN}" value={token} display={`${token.slice(0, 12)}…`} />
                  ) : (
                    <p className="mutedText">{t("zabbix.hostTokenExisting")}</p>
                  )}
                  <p className="mutedText">{t("zabbix.hostSecret")}</p>
                  <h3 className={styles.subTitle}>{t("zabbix.tlsTitle")}</h3>
                  <p>{t("zabbix.tlsText")}</p>
                  {setup.ca_certificate_available && (
                    <div>
                      <button type="button" className="button" onClick={() => download(zabbixApi.downloadCaCertificate)}>
                        {t("zabbix.downloadCa")}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {step === 4 && (
                <div className={styles.stepBody} data-testid="zabbix-step-done">
                  <p>{t("zabbix.doneIntro")}</p>
                  <ul className={styles.list}>
                    <li>{t("zabbix.doneLatestData")}</li>
                    <li>{t("zabbix.doneDiscovery")}</li>
                    <li>{t("zabbix.doneTroubleshoot")}</li>
                  </ul>
                  <h3 className={styles.subTitle}>{t("zabbix.thresholds")}</h3>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t("zabbix.macro")}</th>
                        <th>{t("zabbix.default")}</th>
                        <th>{t("zabbix.meaning")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td><code>{"{$RESURGAM.DATASTORE.PUSED.WARN}"}</code></td>
                        <td>80 %</td>
                        <td>{t("zabbix.macroWarn")}</td>
                      </tr>
                      <tr>
                        <td><code>{"{$RESURGAM.DATASTORE.PUSED.CRIT}"}</code></td>
                        <td>90 %</td>
                        <td>{t("zabbix.macroCrit")}</td>
                      </tr>
                      <tr>
                        <td><code>{"{$RESURGAM.JOB.AGE.MAX}"}</code></td>
                        <td>26 h</td>
                        <td>{t("zabbix.macroJobAge")}</td>
                      </tr>
                      <tr>
                        <td><code>{"{$RESURGAM.DATA.AGE.MAX}"}</code></td>
                        <td>1 h</td>
                        <td>{t("zabbix.macroDataAge")}</td>
                      </tr>
                    </tbody>
                  </table>
                  <p>
                    <a href={MANUAL_URL} target="_blank" rel="noreferrer">
                      {t("zabbix.manualLink")}
                    </a>
                  </p>
                </div>
              )}
            </Wizard>
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
