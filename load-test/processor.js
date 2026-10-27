'use strict';
/*
 * processor.js - helper functions shared by stampede-waiting-room.yml
 * and stampede-direct.yml
 *
 * Artillery has no built-in "keep polling until admitted" step, so the
 * loop condition lives here, along with the custom metrics that make the
 * waiting-room experience visible in Artillery's report:
 *
 *   waiting_room.admitted           how many visitors made it through
 *   waiting_room.gave_up            visitors still waiting when we stopped polling
 *   waiting_room.wait_seconds       histogram: time from join to admission
 *   waiting_room.polls_per_visitor  histogram: status requests each visitor made
 *
 * Nothing here knows which waiting-room OPTION is deployed. It relies
 * only on the shared contract (status "waiting" / "admitted"), so the same
 * file serves options A, B and C.
 */

// Safety valve: stop polling after this many status checks so a broken
// waiting room cannot leave virtual users polling forever. With a
// 5-second poll interval, 120 polls = 10 minutes.
const MAX_POLLS = parseInt(process.env.MAX_POLLS || '120', 10);
const DEFAULT_POLL_SECONDS = 5;

// Real browsers do not poll in lockstep. With an identical interval for everyone, visitors who
// joined together poll together, and because the line admits them together they also ORDER
// together: orders reach the backend in clumps instead of a trickle. So every wait is randomized
// by +/- POLL_JITTER (0.2 = 4 to 6 seconds around a 5-second interval). POLL_JITTER=0 turns it off.
const POLL_JITTER = parseFloat(process.env.POLL_JITTER || '0.2');

// Runs once per visitor, BEFORE they join: start the stopwatch.
function startWaiting(context, events, done) {
  context.vars.joinedAt = Date.now();
  context.vars.polls = 0;
  context.vars.waitStatus = 'waiting';
  // Overwritten by the server's own poll_interval_seconds if join returns one.
  context.vars.pollSeconds = DEFAULT_POLL_SECONDS;
  return done();
}

// Loop condition for `whileTrue`.
//
// GOTCHA: Artillery evaluates `whileTrue` AFTER each pass through the loop
// body (do-while), not before the first one. So the body always runs at
// least once, and this function is called once per COMPLETED poll. That is
// why the counter is incremented here, unconditionally: by the time we are
// called, one more status check has already happened.
function stillWaiting(context, next) {
  context.vars.polls += 1;
  const waiting = context.vars.waitStatus !== 'admitted';
  const underCap = context.vars.polls < MAX_POLLS;
  return next(waiting && underCap);
}

// Runs once per visitor, AFTER the polling loop: report how it went.
function recordOutcome(context, events, done) {
  if (context.vars.waitStatus === 'admitted') {
    events.emit('counter', 'waiting_room.admitted', 1);
    events.emit('histogram', 'waiting_room.wait_seconds',
                (Date.now() - context.vars.joinedAt) / 1000);
    events.emit('histogram', 'waiting_room.polls_per_visitor', context.vars.polls);
  } else {
    events.emit('counter', 'waiting_room.gave_up', 1);
  }
  return done();
}

// Gives each order a realistic total as a real JS number (e.g. 298.16).
//
// WHY A FUNCTION INSTEAD OF "{{ $randomNumber(50, 500) }}.99"?
//   Artillery stringifies the result of template FUNCTIONS: even a
//   whole-field "{{ $randomNumber(50, 500) }}" arrives as the JSON STRING
//   "284", never the number 284 (verified by capturing request bodies).
//   A variable SET BY A PROCESSOR keeps its type when used as a whole
//   field, so "{{ orderTotal }}" arrives as a real JSON number. Real
//   clients send numbers - and a fractional total like 298.16 is exactly
//   what exercises OrderProducer's float -> Decimal conversion for
//   DynamoDB, which a string total would silently skip.
function setOrderTotal(context, events, done) {
  context.vars.orderTotal = Math.round((50 + Math.random() * 450) * 100) / 100;
  return done();
}

// The jittered interval, in seconds: base plus or minus POLL_JITTER (a pure function, easy to test).
function jitteredSeconds(base) {
  const factor = 1 + POLL_JITTER * (2 * Math.random() - 1);
  return Math.round(base * factor * 1000) / 1000;
}

// Runs before EVERY poll and waits the jittered interval.
//
// GOTCHA: this is a function step, not `think:`. Artillery's `think` silently IGNORES decimals -
// a literal `think: 1.5`, or `{{ t }}` holding 1.5, waits 0 seconds (verified); only whole seconds
// work. A setTimeout inside a function step keeps sub-second precision.
function waitBeforePoll(context, events, done) {
  const base = Number(context.vars.pollSeconds) || DEFAULT_POLL_SECONDS;
  const seconds = jitteredSeconds(base);
  context.vars.lastWaitSeconds = seconds;
  setTimeout(done, seconds * 1000);
}

module.exports = { startWaiting, stillWaiting, recordOutcome, setOrderTotal, waitBeforePoll, jitteredSeconds };
