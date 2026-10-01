// The one live channel: the server says "something changed" and nothing more.
// The first message of the first connection is the greeting, not a change; the
// greeting of any later connection stands for whatever was missed while the
// stream was down.
export function openStream(onChange, onError) {
  const es = new EventSource('/api/events');
  let greeted = false;
  es.onmessage = () => {
    if (!greeted) { greeted = true; return; }
    onChange();
  };
  es.onerror = () => onError();
}
