import { useId, type KeyboardEvent, type ReactNode } from "react";

/**
 * Shared accessible navigation for both illustrative Demo and authenticated Live.
 * No data, approvals, network calls or job execution live in this component.
 */
export function ModelDetailTabs<T extends string>({
  tabs,
  value,
  onChange,
  children,
  label = "Model sections",
  panelClassName,
}: {
  tabs: readonly T[];
  value: T;
  onChange: (tab: T) => void;
  children: ReactNode;
  label?: string;
  panelClassName?: string;
}) {
  const instanceId = useId().replaceAll(":", "");
  const activeIndex = tabs.indexOf(value);

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next: number;
    switch (event.key) {
      case "ArrowRight":
        next = (index + 1) % tabs.length;
        break;
      case "ArrowLeft":
        next = (index + tabs.length - 1) % tabs.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tabs.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    onChange(tabs[next]);
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]
      ?.focus();
  }

  return (
    <>
      <div className="tabs model-detail-tabs" role="tablist" aria-label={label}>
        {tabs.map((tab, index) => (
          <button
            type="button"
            key={tab}
            id={`model-tab-${instanceId}-${index}`}
            role="tab"
            aria-selected={value === tab}
            aria-controls={`model-panel-${instanceId}`}
            tabIndex={value === tab ? 0 : -1}
            className={value === tab ? "selected" : ""}
            onClick={() => onChange(tab)}
            onKeyDown={(event) => onKeyDown(event, index)}
          >
            {tab}
          </button>
        ))}
      </div>
      <section
        id={`model-panel-${instanceId}`}
        className={panelClassName}
        role="tabpanel"
        aria-labelledby={`model-tab-${instanceId}-${activeIndex}`}
        tabIndex={0}
      >
        {children}
      </section>
    </>
  );
}
