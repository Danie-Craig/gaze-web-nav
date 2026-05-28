import json

actions = json.load(open('recordings/20260527_205452/actions.json'))
gaze = json.load(open('recordings/20260527_205452/gaze.json'))

for action in actions[:5]:  # check first 5 actions
    t = action['timestamp']
    relevant_gaze = [g for g in gaze if t - 5.0 <= g['t'] <= t]
    print(f"Step {action['step']}: {action['type']} at t={t:.2f}")
    print(f"  Gaze points in 5s before: {len(relevant_gaze)}")
    if relevant_gaze:
        first = relevant_gaze[0]['raw']
        last = relevant_gaze[-1]['raw']
        print(f"  First gaze: {first[:60]}")
        print(f"  Last gaze:  {last[:60]}")
    print()