import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { customersApi } from "../api/resources";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import styles from "./CustomerSwitcher.module.css";

/** Top-bar tenant switcher (raised directly by the user, "to prepare a
 * Customer Panel") -- picking a customer here is a real navigation, not
 * a silent filter: it takes you to that customer's panel
 * (`CustomerPanelPage`), the same way switching accounts in most
 * multi-tenant admin tools jumps you to that account's own dashboard.
 * The selection is also kept in `CustomerContext` (persisted,
 * per-viewer) for future customer-scoped pages to read without their
 * own separate picker.
 */
export function CustomerSwitcher() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { selectedCustomerId, setSelectedCustomerId } = useSelectedCustomer();

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const handleChange = (id: string) => {
    setSelectedCustomerId(id || null);
    if (id) {
      navigate(`/customers/${id}/panel`);
    } else {
      navigate("/");
    }
  };

  return (
    <div className={styles.switcher}>
      <label className={styles.label} htmlFor="customer-switcher">
        {t("customerSwitcher.label")}
      </label>
      <select
        id="customer-switcher"
        className={`select ${styles.select}`}
        value={selectedCustomerId ?? ""}
        onChange={(e) => handleChange(e.target.value)}
        disabled={customersQuery.isLoading}
      >
        <option value="">{t("customerSwitcher.allCustomers")}</option>
        {customersQuery.data?.map((customer) => (
          <option key={customer.id} value={customer.id}>
            {customer.name}
          </option>
        ))}
      </select>
    </div>
  );
}
