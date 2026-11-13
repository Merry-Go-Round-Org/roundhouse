import type { ValidationCheck } from "./validation";

/** Issue #32 report classification only; never used to authorize a decision. */
export function Gate3Requirements({ checks }: { checks: ValidationCheck[] }) {
  const failures = checks.filter(check => check.status === "FAIL");
  const limitations = checks.filter(check => check.required &&
    (check.status === "REVIEW" || check.status === "NOT_ASSESSABLE"));
  return <section aria-label="Gate 3 requirements" className="lifecycle-card">
    <h3>Gate 3 requirements</h3>
    <section aria-label="Hard failures">
      <h4>Hard failures</h4>
      {failures.length ? <><p>These failures must be resolved before acceptance.</p><ul>
        {failures.map(check => <li key={check.check_id}><strong>{check.check_id}</strong> · {check.status}<p>{check.summary}</p></li>)}
      </ul></> : <p>No FAIL checks in this report.</p>}
    </section>
    <section aria-label="Required limitations">
      <h4>Required limitations</h4>
      {limitations.length ? <><p>These require a written condition for each check under the conditional-acceptance policy. They are not passes.</p><ul>
        {limitations.map(check => <li key={check.check_id}><strong>{check.check_id}</strong> · {check.status}<p>{check.summary}</p></li>)}
      </ul></> : <p>No required REVIEW or NOT_ASSESSABLE checks in this report.</p>}
    </section>
    <p>Open Review queue or the chat review cards to inspect permitted decisions and acknowledge each limitation. This report summary does not grant approval or change check statuses.</p>
  </section>;
}
