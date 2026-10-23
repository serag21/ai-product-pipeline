#!/usr/bin/env python3
"""Bounded, resumable FLUX.2 + Qwen production run for the Little Haunts library."""
from __future__ import annotations
import argparse, json, os, sys, time, subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "src"))
from comfy.client import ComfyUIClient
from generate_product import (WORKFLOW_PATH, build_prompt, generation_settings, load_product,
                              resolve_product_root, validate_workflow, quality_check)
from product_review_worker import (OpenAICompatibleReviewer, add_review_feedback, criteria_for,
                                   enforce_image_checks, generate_one, validate_review)
from local_services import DEFAULT_COMFY, ensure_comfy, ensure_ollama

STYLE = ("Original Little Haunts Club cozy-cute-spooky storybook coloring page, portrait composition, "
         "crisp confident black ink contours on pure white paper, clear rounded silhouettes, broad open "
         "coloring regions, moderate scene-specific detail, generous printable margin and simple rounded frame. "
         "Every large surface remains white inside clean outlines; solid black only for tiny pupils and accents. "
         "No grayscale, shading, crosshatching, solid black clothing/animals/props, gradients, lettering, or pseudo-text. "
         "Make the character's hands, feet, tools, supports, and contact points anatomically and physically clear.")

REPLACEMENTS = [
 (2,"Patchwork Monster Builds a Bird Feeder","A square-headed stitched monster child with two neck bolts builds an open tray bird feeder.",[
  "Wide garden workbench composition: the monster braces the post on the bench and screws a shallow tray beneath a tiny roof; robin waits on a fence rail, boards lean against the bench.",
  "Low ground-level garden view: monster kneels beside a planted post and taps the feeder roof into place; bird watches from a nearby watering can, flowers frame the opposite corner."]),
 (3,"The Plague-Doctor Crow's Apothecary","A small friendly white-feathered crow in a simple beaked mask prepares a remedy at a cottage apothecary.",[
  "Side-on counter scene: crow grinds one leaf in a broad mortar while three large bottles sit on a low counter; hanging herb bundles attach to a beam, a stool supports a satchel.",
  "Overhead-diagonal worktable composition: crow labels no text, instead ties a blank tag to one bottle; open cabinet shelves and a potted herb establish the shop, no windows."]),
 (5,"Mummy's Tangled Present","A friendly mummy wraps one present, accidentally using its loose bandage as the ribbon.",[
  "Seated floor composition: mummy holds a broad paper roll across its lap and loops one connected bandage around a low square gift; tape and scissors rest on a separate stool.",
  "Standing worktable view: mummy leans over a large flat parcel on a sturdy table, bandage trails from its sleeve into a bow; paper roll, two parcels, and a blank wall shelf."]),
 (7,"Vampire's Midnight Bakery","A young vampire baker decorates one tray of moon-shaped cookies in a small cottage bakery.",[
  "Front counter composition: vampire slides a tray onto a clearly supported cooling rack; oven door is closed, flour bowl and rolling pin sit on the counter, bat-shaped cookie tin below.",
  "Three-quarter kitchen view: vampire pipes icing with a small bag held in both hands over a wide cookie tray; oven mitt hangs from a wall hook, mixing bowl rests on a stool."]),
 (8,"Ghost-and-Cat Blanket-Fort Night","A gentle sheet ghost and a curious cat read together inside a blanket fort.",[
  "Low cozy room view: blanket drapes between a sofa back and a sturdy chair, ghost and cat share a large open book on floor cushions; flashlight lies beside a popcorn bowl.",
  "Elevated diagonal view: ghost holds a book while cat peeks through the fort's broad arched entrance; blanket is clipped to two visible furniture supports, lantern rests outside."]),
 (13,"Little Grim Reaper's Flower Delivery","A tiny friendly grim reaper delivers a potted flower to a neighbor's cottage gate.",[
  "Sidewalk composition: reaper carries a two-handled flowerpot toward a low gate; scythe is safely strapped closed to a small satchel, path stones lead to a porch step.",
  "Porch-level view: reaper sets a broad flowerpot on a clearly supported doorstep and rings a bell; closed scythe leans in a ground stand beside a doormat and wall lantern."]),
]

SCENES = [
 ("Owl Postmistress Sorts Moon Mail","A tiny witch postmistress and her round owl sort blank envelopes at a village post counter.","Interior counter view; owl steadies a basket of envelopes while the witch reaches to a cubby shelf; satchel hangs from a wall peg, parcels rest on the floor."),
 ("Skeleton Astronomer Maps the Sky","A childlike skeleton astronomer charts stars from a cozy rooftop observatory.","Seated at a sturdy telescope on a three-legged stand; open star chart on a supported stool, domed roof and one hanging lantern, no filled sky."),
 ("Zombie Chef Stirs Harvest Soup","A cheerful little zombie chef stirs a wide soup pot on a safe cottage hearth.","Three-quarter kitchen scene; pot rests on a brick hearth, ladle held clearly, bread loaf on a sideboard, apron has broad blank panels."),
 ("Bat Librarian Repairs a Storybook","A bespectacled bat librarian repairs a torn storybook at a rolling book cart.","Bat sits on a low stool, wings folded into outlined colorable panels, paws hold book and paste brush; cart wheels touch floor, stacked books on shelf."),
 ("Werewolf Pup Performs Shadow Puppets","A fluffy werewolf pup performs a moon-rabbit shadow puppet for two tiny mouse friends.","Pup stands behind a simple tabletop screen, holds one clear rod puppet, mice sit on cushions; lantern rests on a broad floor crate; all shadows are outlined shapes, not dark fills."),
 ("Black Cat Botanist Repots a Mandrake","A black-cat botanist repots a sleepy little mandrake in a greenhouse.","Cat kneels beside a broad pot with a friendly leafy root peeking out; trowel and watering can rest on a low bench, three large plant leaves, open window frame."),
 ("Sea Monster Runs a Bubble Bath","A shy baby sea monster enjoys a bubble bath in a clawfoot tub while a rubber duck floats nearby.","Full tub with visible feet on floor, monster's arms rest over rim, bubbles are separate outlined circles; towel hangs on a connected wall rail, bath stool supports soap."),
 ("Cyclops Winds a Music Box","A gentle one-eyed cyclops winds a tiny music box on a repair bench.","Cyclops uses one hand on the side key and one to steady the open box; gears are few and large, lamp rests on bench, toolbox sits below with lid open."),
 ("Vampire Violinist Tunes for a Bat Quartet","A young vampire tunes a violin before a tiny seated bat quartet.","Vampire stands on a small rug beside a music stand; bow in one hand and violin in the other, four bats sit on a connected bench, blank music sheets."),
 ("Mummy Florist Makes a Night Bouquet","A friendly mummy florist arranges three oversized moonflowers in a wide vase.","Mummy stands behind a florist counter with broad flower stems and one open ribbon spool; shears lie closed on the counter, two empty pots rest on a floor shelf."),
 ("Gargoyle Repairs a Rooftop Weather Vane","A young gargoyle carefully repairs a crooked weather vane on a safe low rooftop.","Gargoyle sits astride a broad roof ridge while tightening a bolt with a small wrench; ladder reaches roof edge, tool pouch attached at waist, chimney behind."),
 ("Patchwork Monster Designs a Kite","A patchwork monster child makes a diamond kite in a sunny attic before Halloween.","Monster sits at an attic floor workmat, ties a long tail to the kite; paper, two light rods, and thread spool stay on mat, open dormer window and trunk behind."),
 ("Haunted Doll Paints a Mini Portrait","A kindly haunted doll paints a tiny portrait of its stuffed rabbit friend.","Doll sits at a sturdy easel with broad blank canvas, one brush in hand; rabbit sits on a stool, paint pots on a side table, framed wall art kept simple."),
 ("Little Imp Raises the Theatre Curtain","A tiny imp stagehand raises the curtain for a puppet show in a neighborhood playhouse.","Imp pulls a clearly attached side rope; curtain gathers at visible hooks, two simple puppets wait on a supported stage, audience seats and one floor lantern."),
 ("Moth Keeper Tends the Porch Lantern","A gentle moth creature cleans a cottage porch lantern so lost fireflies can find home.","Moth stands on a stable step stool and wipes a broad lantern attached to the wall; three outlined fireflies hover nearby, door mat and potted fern anchor the porch."),
]

def plans() -> list[dict[str,Any]]:
    out=[]
    for page,title,hero,layouts in REPLACEMENTS:
        for n,layout in enumerate(layouts,1):
            out.append({"id":f"replacement-p{page}-{n}","category":"replacement","page":page,"title":f"Page {page} · {title} · Option {n}","hero":hero,"layout":layout,"seed_offset":page*100+n})
    for n,(title,hero,layout) in enumerate(SCENES,1):
        count=3 if n<=9 else 2
        compositions=[layout,
          layout+" Compose as a close character-centered view with foreground prop grouping on the left and the room's key support visible.",
          layout+" Compose as a wider diagonal view with the main activity in the right half and a grounded setting detail in the left half."]
        for v in range(count):
            out.append({"id":f"new-{n:02d}-{v+1}","category":"new_scene","title":f"New {n:02d} · {title} · Layout {v+1}","hero":hero,"layout":compositions[v],"seed_offset":10000+n*20+v})
    return out

def atomic(path:Path,obj:Any):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding="utf-8"); os.replace(tmp,path)

def dhash(path:Path)->int:
    from PIL import Image
    im=Image.open(path).convert("L").resize((9,8)); p=list(im.getdata()); out=0
    for y in range(8):
        for x in range(8): out=(out<<1)|(p[y*9+x]>p[y*9+x+1])
    return out

def gpu_snapshot() -> tuple[int,int] | None:
    try:
        result=subprocess.run(["nvidia-smi","--query-gpu=memory.free,utilization.gpu","--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=10,check=True)
        free,util=result.stdout.strip().splitlines()[0].split(",")
        return int(free.strip())*1024**2,int(util.strip())
    except Exception:
        return None

def wait_gpu_headroom(status_path:Path, timeout:int=4*60*60) -> None:
    """Wait for existing foreground GPU work; do not compete with it or risk a VRAM overcommit."""
    deadline=time.monotonic()+timeout; good=0; last=None
    while time.monotonic()<deadline:
        sample=gpu_snapshot()
        if sample is None:
            raise RuntimeError("Cannot read NVIDIA GPU telemetry with nvidia-smi; refusing to load another large model without a hardware safety check.")
        else:
            free,util=sample; last=(free,util)
            good=good+1 if free>=22*1024**3 and util<45 else 0
        if good>=3: return
        atomic(status_path,{"status":"waiting_for_safe_gpu_headroom","message":"A foreground GPU workload is active. The batch is waiting for at least 22 GiB free VRAM and low utilization before loading FLUX or Qwen.","gpu_free_mib":round(last[0]/1024**2) if last else None,"gpu_utilization_percent":last[1] if last else None,"updated_at":datetime.now(timezone.utc).isoformat()})
        print(f"Waiting for safe GPU headroom: {round(last[0]/1024**2)} MiB free, {last[1]}% load",flush=True)
        time.sleep(20)
    raise RuntimeError(f"Safe GPU headroom was unavailable for {timeout//60} minutes; batch stopped without competing with the active workload. Last sample: {last}")

def run(args):
    product_root=resolve_product_root(args.product); product=load_product(product_root)
    run_dir=product_root/"review_runs"/args.run_id; library_dir=run_dir/"candidate_library"; library_dir.mkdir(parents=True,exist_ok=True)
    db=run_dir/"candidate_library.json"; dashboard_status_path=run_dir/"dashboard_status.json"; existing=json.loads(db.read_text(encoding="utf-8")) if db.exists() else {"items":[]}
    records={x["id"]:x for x in existing.get("items",[]) if x.get("category")!="approved"}; allplans=plans(); max_total=60; max_retries=9
    human=json.loads((run_dir/"dashboard_state.json").read_text(encoding="utf-8")); prior_state=json.loads((run_dir/"state.json").read_text(encoding="utf-8"))
    approved=[]
    for page,entry in human.get("pages",{}).items():
        if entry.get("status")!="approved": continue
        file=entry.get("candidate_file"); cand=next((c for c in prior_state.get("candidates",{}).get(str(page),{}).get("attempts",[]) if c.get("file")==file),{})
        approved.append({"id":f"approved-p{page}","category":"approved","title":f"Approved page {int(page):02d}","file":file,"status":"human approved · locked","rank":1000,"surface":True,"qa":cand.get("qa",{}),"review":cand.get("review",{})})
    existing.pop("error",None)
    existing.update({"status":"running","planned":len(allplans),"generated":sum(x.get("file") is not None for x in records.values()),"completed":sum(x.get("review") is not None for x in records.values()),"budget":max_total,"started_at":existing.get("started_at",datetime.now(timezone.utc).isoformat()),"items":approved+list(records.values())}); atomic(db,existing)
    atomic(dashboard_status_path,{"status":"candidate_library_running","message":"Generating and visually reviewing the bounded replacement and new-scene collection.","generated":existing["generated"],"planned":len(allplans),"updated_at":datetime.now(timezone.utc).isoformat()})
    cfg=json.loads((product_root/"factory_config.json").read_text(encoding="utf-8")); vc=cfg["vision"]
    server=cfg.get("comfy",{}).get("server","http://127.0.0.1:8188"); install=cfg.get("comfy",{}).get("install",str(DEFAULT_COMFY))
    ensure_comfy(server,install); ensure_ollama(vc["endpoint"],vc["model"])
    client=ComfyUIClient(server); reviewer=OpenAICompatibleReviewer(vc["endpoint"],vc["model"],os.getenv(vc.get("api_key_env","VISION_API_KEY")),unload_endpoint=vc.get("unload_endpoint"),max_tokens=vc.get("max_tokens",2048),reasoning_effort=vc.get("reasoning_effort"))
    client.health(); wait_gpu_headroom(dashboard_status_path); reviewer.check(); workflow=client.load_workflow(WORKFLOW_PATH); validate_workflow(workflow)
    generated=existing["generated"]
    def one(plan, retry=False):
        nonlocal generated
        prior=records.get(plan["id"],{}) if retry else {}
        attempt=(max(int(prior.get("generation_count",0)),max((int(r.get("generation_count",0)) for r in records.values() if r.get("retry_of")==plan["id"]),default=0))+1) if retry else int(records.get(plan["id"],{}).get("generation_count",0))+1
        row_id=f"{plan['id']}-retry-{attempt:02d}" if retry else plan["id"]
        row=records.setdefault(row_id,{**plan,"id":row_id,"retry_of":plan["id"] if retry else None,"title":plan["title"]+(f" · Retry {attempt-1}" if retry else ""),"status":"queued","file":None,"review":None,"qa":None})
        if row.get("review") is not None: return
        if generated>=max_total: return
        dest=library_dir/f"{plan['id']}-try-{attempt:02d}.png"
        if dest.exists(): dest=library_dir/f"{plan['id']}-try-{attempt:02d}-resume-{int(time.time())}.png"
        feedback=""
        if retry and prior.get("review"):
            rr=prior["review"]; feedback="Correct these specific observed issues: "+"; ".join([rr.get("summary","")]+rr.get("defects",[])+[rr.get("retry_prompt","")])
        item={"page":plan.get("page",100+allplans.index(plan)),"variant_id":plan["id"],"scene_title":plan["title"],"label":plan["title"],"qa_focus":f"Check appealing original composition, physically supported props, clear anatomy, large colorable open surfaces, clean line art and distinct scene layout. Intended activity: {plan['hero']} Layout: {plan['layout']}"}
        prompt=f"{plan['hero']} {plan['layout']} {STYLE}"
        if plan.get("page") is not None:
            latest_human=json.loads((run_dir/"dashboard_state.json").read_text(encoding="utf-8"))
            critique=str(latest_human.get("pages",{}).get(str(plan["page"]),{}).get("note","")).strip()
            if critique:
                prompt += f"\n\nHuman art-direction note for this rejected page: {critique}. Treat this instruction as a hard creative constraint in every replacement option; correct the named defect and change the composition meaningfully."
        if feedback: prompt=add_review_feedback(prompt,feedback)
        seed=900000+plan["seed_offset"]+attempt*7919
        row.update({"status":"generating","generation_count":attempt,"updated_at":datetime.now(timezone.utc).isoformat()}); existing.update({"items":approved+list(records.values()),"generated":generated,"active":plan["id"]}); atomic(db,existing)
        atomic(dashboard_status_path,{"status":"candidate_library_running","message":f"Generating {plan['title']}","generated":generated,"planned":len(allplans),"updated_at":datetime.now(timezone.utc).isoformat()})
        reviewer.unload(); wait_gpu_headroom(dashboard_status_path); client.health()
        result=generate_one(client,item,product,workflow,dest,item["page"],attempt,args.run_id.replace("-","_"),prompt,seed,args.timeout)
        generated+=1; qa=result["qa"]
        if qa.get("pass"):
            client.free_memory(); wait_gpu_headroom(dashboard_status_path)
            criteria=criteria_for({"title":product.get("title"),"product_id":product["product_id"]},item)+" Also judge charm, originality, composition balance, actual drawing coherence, and whether this scene adds a distinct activity to a varied collection. Do not give 100 for merely matching a prompt."
            review=enforce_image_checks(validate_review(reviewer.review(dest,criteria,"")),dest)
        else: review={"decision":"retry","score":0,"summary":"Image integrity check failed.","defects":["Resolution or contrast QA failed."],"retry_prompt":"Regenerate with clean bold outlines and broad white areas.","checks":{}}
        row.update({"file":str(dest),"prompt_id":result.get("prompt_id"),"seed":seed,"qa":qa,"review":review,"status":"reviewed" if review["decision"] in {"accept","manual"} else "needs_retry","updated_at":datetime.now(timezone.utc).isoformat(),"hash":dhash(dest)})
        existing.update({"items":approved+list(records.values()),"generated":generated,"completed":sum(x.get("review") is not None for x in records.values()),"active":None,"updated_at":datetime.now(timezone.utc).isoformat()}); atomic(db,existing)
        atomic(dashboard_status_path,{"status":"candidate_library_running","message":f"Reviewed {plan['title']}","generated":generated,"planned":len(allplans),"updated_at":datetime.now(timezone.utc).isoformat()})
        print(f"[{generated}/{max_total}] {plan['id']} {review['decision']} {review['score']} {review['summary'][:180]}",flush=True)
    try:
        for plan in allplans:
            one(plan)
            if generated>=max_total: break
        # At most one defect-directed second look for up to nine weakest scenes.
        retried_successfully={r.get("retry_of") for r in records.values() if r.get("retry_of") and r.get("review",{}).get("decision")=="accept"}
        candidates=[r for r in records.values() if r.get("id") not in retried_successfully and r.get("retry_of") not in retried_successfully and r.get("review",{}).get("decision")=="retry" and r.get("generation_count",0)<2]
        # Restore the human-rejected page options first, then spend remaining
        # retries on weak new-scene concepts, including visual QA overrides.
        candidates.sort(key=lambda r:(r.get("category")!="replacement",r.get("review",{}).get("score",0),r["id"]))
        for row in candidates[:max_retries]:
            if generated>=max_total: break
            one(row,True)
        # Rank with explicit printable QA and Qwen, plus a visual near-duplicate penalty.
        ranked=sorted(records.values(),key=lambda r:(r.get("review",{}).get("decision")=="accept",r.get("qa",{}).get("pass",False),r.get("review",{}).get("score",0)),reverse=True)
        selected=[]; used=set()
        for row in ranked:
            if not row.get("file") or not row.get("qa",{}).get("pass"): continue
            score=int(row.get("review",{}).get("score",0)); dist=min(((int(row.get("hash",0))^int(x.get("hash",0))).bit_count() for x in selected),default=64)
            row["diversity_score"]=dist; row["rank"]=round(score+min(10,dist/4)+(8 if row.get("review",{}).get("decision")=="accept" else 0),2)
            if dist<5 and selected: row["surface"]=False
            elif row.get("review",{}).get("decision")=="accept": row["surface"]=True; selected.append(row)
            else: row["surface"]=False
            row["status"]="strong_candidate" if row["surface"] else row.get("status","archive")
        # Ensure approved source pages remain visible and immutable in the candidate library.
        human=json.loads((run_dir/"dashboard_state.json").read_text(encoding="utf-8")); state=json.loads((run_dir/"state.json").read_text(encoding="utf-8"))
        combined=approved+list(records.values()); existing.update({"status":"complete","generated":generated,"completed":sum(x.get("review") is not None for x in records.values()),"items":combined,"active":None,"completed_at":datetime.now(timezone.utc).isoformat()}); atomic(db,existing)
        atomic(dashboard_status_path,{"status":"candidate_library_complete","message":f"{generated} images generated and reviewed; final human selection remains required.","generated":generated,"planned":len(allplans),"updated_at":datetime.now(timezone.utc).isoformat()})
    except KeyboardInterrupt:
        existing.update({"status":"paused","generated":generated,"items":approved+list(records.values()) if 'approved' in locals() else list(records.values()),"active":None}); atomic(db,existing); atomic(dashboard_status_path,{"status":"candidate_library_paused","generated":generated,"planned":len(allplans),"message":"Batch paused safely; saved images and review state are resumable."}); raise
    except Exception as exc:
        existing.update({"status":"error","error":str(exc),"generated":generated,"items":approved+list(records.values()),"active":None,"updated_at":datetime.now(timezone.utc).isoformat()}); atomic(db,existing); atomic(dashboard_status_path,{"status":"candidate_library_error","error":str(exc),"generated":generated,"planned":len(allplans),"updated_at":datetime.now(timezone.utc).isoformat()}); raise
    print(f"DONE: {generated} images; library at {db}",flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--product",default="products/little-haunts-club-spooky-neighbors-15"); p.add_argument("--run-id",default="factory-main"); p.add_argument("--timeout",type=int,default=1800); args=p.parse_args()
    run_dir=resolve_product_root(args.product)/"review_runs"/args.run_id; run_dir.mkdir(parents=True,exist_ok=True)
    batch_log=(run_dir/"expanded_batch.log").open("a",encoding="utf-8",buffering=1)
    sys.stdout=batch_log; sys.stderr=batch_log
    lock=(run_dir/"expanded_batch.lock").open("a+b")
    try:
        if os.name=="nt":
            import msvcrt
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,BlockingIOError):
        print("Expanded production batch is already running; leaving its saved state untouched.",flush=True); return 0
    cfg=json.loads((resolve_product_root(args.product)/"factory_config.json").read_text(encoding="utf-8")); vc=cfg["vision"]
    server=cfg.get("comfy",{}).get("server","http://127.0.0.1:8188")
    install=cfg.get("comfy",{}).get("install",str(DEFAULT_COMFY))
    for restart in range(5):
        try: return run(args)
        except Exception as exc:
            print(f"Recoverable batch failure {restart+1}/5: {exc}",file=sys.stderr,flush=True)
            if restart==4: return 1
            time.sleep(15)
            ensure_comfy(server,install)
            try:
                ComfyUIClient(server).free_memory(timeout=120)
            except Exception as cleanup_exc:
                print(f"ComfyUI memory cleanup during recovery did not complete: {cleanup_exc}",file=sys.stderr,flush=True)
            ensure_ollama(vc["endpoint"],vc["model"])
    return 1
if __name__=="__main__":
    try: main()
    except Exception as exc: print(f"BATCH ERROR: {exc}",file=sys.stderr,flush=True); raise
