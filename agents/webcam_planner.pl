%% =====================================================================
%% DALI2 Webcam-Based Tourist Planner
%%
%% Two agents cooperate to optimise monument visits based on real-time
%% crowd analysis from public webcams (images analysed by GPT-4o).
%%
%% Agents:
%%   planner   — receives crowd_report events from the Python bridge,
%%               maintains beliefs about crowd levels per monument,
%%               and computes an optimal visit schedule (least crowded
%%               first).  Can also query the AI oracle for high-level
%%               travel advice.
%%   monitor   — logs all events and provides a situational awareness
%%               overview via internal events.
%%
%% External events (published by webcam_bridge.py via LINDA):
%%   crowd_report(WebcamId, Name, CrowdLevel, Weather,
%%                CrowdDesc, Visibility, WeatherDesc, Lat, Lon)
%%   analysis_error(WebcamId, Reason)
%%
%% User-injectable events (via REST / Web UI):
%%   plan_visit          — trigger route planning
%%   request_scan        — ask the bridge to refresh all webcams now
%% =====================================================================


%% =====================================================================
%% PLANNER
%% =====================================================================

:- agent(planner, [cycle(2)]).

%% ── Initial beliefs ──────────────────────────────────────────────────
believes(monuments([])).
believes(scan_count(0)).

%% ── Told rules — priority queue ──────────────────────────────────────
told(_, crowd_report(_,_,_,_,_,_,_,_,_), 200) :- true.
told(_, analysis_error(_,_),              100) :- true.
told(_, plan_visit,                       150) :- true.
told(_, request_scan,                      50) :- true.
told(_, ai_advice(_),                     100) :- true.

%% ── Tell rules ───────────────────────────────────────────────────────
tell(_, _, request_scan)                  :- true.
tell(_, _, log_event(_,_,_))              :- true.
tell(_, _, visit_plan(_))                 :- true.

%% ── Past event lifetime ──────────────────────────────────────────────
past_event(crowd_report(_,_,_,_,_,_,_,_,_), 600).
remember_event(crowd_report(_,_,_,_,_,_,_,_,_), 3600).
remember_event_mod(crowd_report(_,_,_,_,_,_,_,_,_), number(50), last).

%% ── Reactive rules ──────────────────────────────────────────────────

%% Process a crowd report from the webcam bridge.
crowd_reportE(WId, Name, Crowd, Weather, Desc, Vis, WDesc, Lat, Lon) :>
    log("Report: ~w (~w) crowd=~w weather=~w vis=~w",
        [Name, WId, Crowd, Weather, Vis]),
    %% Update belief: retract old monument data, assert new
    ( believes(monument(WId, _, _, _, _, _, _, _, _))
    ->  retract_belief(monument(WId, _, _, _, _, _, _, _, _))
    ;   true
    ),
    assert_belief(monument(WId, Name, Crowd, Weather, Desc, Vis, WDesc, Lat, Lon)),
    %% Update monument list
    ( believes(monuments(L)),
      \+ member(WId, L)
    ->  retract_belief(monuments(L)),
        assert_belief(monuments([WId | L]))
    ;   true
    ),
    %% Track scan count
    believes(scan_count(N)),
    retract_belief(scan_count(N)),
    N1 is N + 1,
    assert_belief(scan_count(N1)),
    send(monitor, log_event(crowd_update, planner, [WId, Name, Crowd, Weather])),
    do(compute_plan).

%% Handle analysis errors from the bridge.
analysis_errorE(WId, Reason) :>
    log("Analysis error for ~w: ~w", [WId, Reason]),
    send(monitor, log_event(analysis_error, planner, [WId, Reason])).

%% User requests a visit plan.
plan_visitE :>
    log("Planning visit route..."),
    do(compute_plan).

%% User requests a fresh scan.
request_scanE :>
    log("Forwarding scan request to webcam bridge"),
    send(webcam_bridge, request_scan).

%% ── Action: compute optimal visit plan ──────────────────────────────

compute_planA :-
    findall(
        crowd(Crowd, WId, Name, Weather, Lat, Lon),
        believes(monument(WId, Name, Crowd, Weather, _, _, _, Lat, Lon)),
        Monuments
    ),
    ( Monuments = []
    ->  log("No monument data yet. Request a scan first."),
        send(monitor, log_event(plan_failed, planner, no_data))
    ;   sort(Monuments, Sorted),
        log("=== VISIT PLAN (least crowded first): ~w ===", [Sorted]),
        ( believes(current_plan(_)) -> retract_belief(current_plan(_)) ; true ),
        assert_belief(current_plan(Sorted)),
        send(monitor, log_event(plan_computed, planner, Sorted))
    ).



%% ── Condition-action: alert when crowd is very high ─────────────────
believes(monument(WId, Name, Crowd, _, _, _, _, _, _)), Crowd >= 8 :<
    log("HIGH CROWD ALERT: ~w (~w) has crowd level ~w!", [Name, WId, Crowd]),
    send(monitor, log_event(high_crowd_alert, planner, [WId, Name, Crowd])).



%% =====================================================================
%% MONITOR
%% =====================================================================

:- agent(monitor, [cycle(2)]).

%% ── Told rules ───────────────────────────────────────────────────────
told(_, log_event(_,_,_), 50) :- true.
told(_, crowd_report(_,_,_,_,_,_,_,_,_), 100) :- true.

%% ── Past event lifetime ──────────────────────────────────────────────
past_event(log_event(_,_,_), 300).
remember_event(log_event(_,_,_), 3600).
remember_event_mod(log_event(_,_,_), number(100), last).

%% ── Reactive rules ──────────────────────────────────────────────────

log_eventE(Type, Source, Details) :>
    log("[MONITOR] ~w from ~w: ~w", [Type, Source, Details]),
    assert_belief(logged(Type, Source, Details)).

%% ── Internal event: periodic status summary ──────────────────────────
status_summaryI :>
    findall(logged(T,S,D), believes(logged(T,S,D)), Logs),
    length(Logs, N),
    log("[MONITOR] Status: ~w events logged", [N]).
internal_event(status_summary, 60, forever, true, forever).

%% ── Periodic heartbeat ──────────────────────────────────────────────
every(30, log("[MONITOR] heartbeat")).
