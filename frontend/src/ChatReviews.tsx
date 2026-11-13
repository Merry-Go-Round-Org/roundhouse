import { useState } from "react";
import { ReviewQueue } from "./ReviewQueue";
export function ChatReviews({modelId}:{modelId:string|null}) {
  const [open,setOpen]=useState(false);
  return <section className="chat-reviews" aria-label="Chat review cards">
    <button type="button" aria-expanded={open} onClick={()=>setOpen(v=>!v)}>
      {open?"Hide review cards":"Open review cards"}
    </button>
    {open&&<>
      <p>{modelId?`Reviews for model ${modelId}.`:"General chat: reviews across models."} Select the exact review to inspect.
        The current chat notice does not identify its review, so these cards are not automatically linked to a paused turn.</p>
      <p>Only an explicit authenticated review submission records a decision. Chat text never approves it.
        After saving, refresh saved chat; automatic review-to-chat resumption is not assumed.</p>
      <ReviewQueue key={modelId??"all"} modelId={modelId??undefined}/>
    </>}
  </section>;
}
