import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import styles from "./Wizard.module.css";

export interface WizardStep {
  id: string;
  title: string;
}

interface WizardProps {
  steps: WizardStep[];
  currentStep: number;
  /** Called when the user clicks an already-completed step's label to
   * jump back to it. Steps ahead of the current one aren't clickable. */
  onStepClick: (index: number) => void;
  canAdvance: boolean;
  onBack: () => void;
  onNext: () => void;
  nextLabel: string;
  children: ReactNode;
}

/** A small, restore-agnostic step-sequencing shell — no state machine
 * library, plain props-driven navigation (matches this codebase's
 * consistently minimal-dependency style, ADR 0008). First built for the
 * Guided Restore wizard (manifest §26.7); kept generic since the
 * Recovery Lab UI (same section) will likely want the same shell later.
 */
export function Wizard({ steps, currentStep, onStepClick, canAdvance, onBack, onNext, nextLabel, children }: WizardProps) {
  const { t } = useTranslation();
  const isLastStep = currentStep === steps.length - 1;

  return (
    <div className={styles.wizard}>
      <ol className={styles.stepList}>
        {steps.map((step, index) => {
          const isCurrent = index === currentStep;
          const isCompleted = index < currentStep;
          const isClickable = isCompleted;
          return (
            <li
              key={step.id}
              className={[
                styles.step,
                isCurrent ? styles.stepCurrent : "",
                isCompleted ? styles.stepCompleted : "",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              {isClickable ? (
                <button type="button" className={styles.stepButton} onClick={() => onStepClick(index)}>
                  <span className={styles.stepNumber}>{index + 1}</span>
                  <span className={styles.stepTitle}>{step.title}</span>
                </button>
              ) : (
                <span className={styles.stepButton}>
                  <span className={styles.stepNumber}>{index + 1}</span>
                  <span className={styles.stepTitle}>{step.title}</span>
                </span>
              )}
            </li>
          );
        })}
      </ol>

      <div className={styles.content}>{children}</div>

      <div className={styles.actions}>
        <button type="button" className="button" onClick={onBack} disabled={currentStep === 0}>
          {t("common.back")}
        </button>
        <button type="button" className="button buttonPrimary" onClick={onNext} disabled={!canAdvance}>
          {isLastStep ? nextLabel : t("common.next")}
        </button>
      </div>
    </div>
  );
}
