import uuid, subprocess, json, re
from pathlib import Path
from urllib.parse import urlparse
import requests
from fastapi import FastAPI,UploadFile,File,Form,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
ROOT=Path(__file__).parent; TMP=ROOT/"tmp"; OUT=ROOT/"outputs"; TMP.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)
app=FastAPI(title="GTA SHADOW X API")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
app.mount("/outputs",StaticFiles(directory=OUT),name="outputs")
def cmd(a):
 p=subprocess.run(a,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 if p.returncode: raise RuntimeError(p.stderr[-1200:])
 return p.stdout
@app.get("/")
def home(): return {"status":"online","app":"GTA SHADOW X"}
@app.post("/api/clip")
async def clip(file:UploadFile|None=File(None),media_url:str|None=Form(None),count:int=Form(10),duration:int=Form(30),aspect:str=Form("9:16"),method:str=Form("balanced")):
 if not 1<=count<=30 or duration not in [15,30,45,60] or aspect not in ["9:16","1:1","16:9"]: raise HTTPException(400,"Invalid clip settings")
 if bool(file)==bool(media_url): raise HTTPException(400,"Provide one video file or direct media URL")
 job=uuid.uuid4().hex; src=TMP/(job+".mp4")
 try:
  if file:
   if file.content_type and not file.content_type.startswith("video/"): raise HTTPException(400,"Upload a video file")
   n=0
   with src.open("wb") as w:
    while b:=await file.read(1024*1024):
     n+=len(b)
     if n>500*1024*1024: raise HTTPException(413,"Maximum upload is 500 MB")
     w.write(b)
  else:
   if urlparse(media_url).scheme!="https": raise HTTPException(400,"HTTPS direct video URL required")
   r=requests.get(media_url,stream=True,timeout=20); r.raise_for_status()
   if not r.headers.get("content-type","").startswith("video/"): raise HTTPException(400,"URL must directly return a video file")
   n=0
   with src.open("wb") as w:
    for b in r.iter_content(1024*1024):
     if b:
      n+=len(b)
      if n>500*1024*1024: raise HTTPException(413,"Maximum source is 500 MB")
      w.write(b)
  meta=json.loads(cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(src)])); total=float(meta["format"]["duration"])
  if total<duration: raise HTTPException(400,"Video is shorter than selected clip duration")
  marks=[]
  if method=="balanced":
   try: marks=[float(x) for x in re.findall(r"pts_time:([0-9.]+)",cmd(["ffmpeg","-i",str(src),"-vf","select='gt(scene,0.28)',showinfo","-an","-f","null","-"]))]
   except: pass
  candidates=[max(0,min(total-duration,t-duration/2)) for t in marks if duration/2<=t<=total-duration/2]
  if len(candidates)<count: candidates=[i*(total-duration)/max(1,count-1) for i in range(count)]
  starts=[]
  for i in range(count):
   target=i*(total-duration)/max(1,count-1); pick=min(candidates,key=lambda x:abs(x-target))
   starts.append(pick if all(abs(pick-x)>duration*.5 for x in starts) else target)
  vf={"9:16":"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920","1:1":"scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080","16:9":"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"}[aspect]
  clips=[]
  for i,st in enumerate(starts):
   name=f"{job}_{i+1}.mp4"; dest=OUT/name
   cmd(["ffmpeg","-y","-ss",str(st),"-i",str(src),"-t",str(duration),"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","24","-c:a","aac","-movflags","+faststart",str(dest)])
   clips.append({"start":st,"end":st+duration,"url":"/outputs/"+name})
  return {"clips":clips,"note":"Scene-change/even distribution MVP, not semantic AI highlights."}
 except HTTPException: raise
 except Exception as e: raise HTTPException(500,str(e))
 finally: src.unlink(missing_ok=True)
