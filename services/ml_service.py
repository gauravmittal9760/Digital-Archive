"""Lightweight, data-driven ML for Digital Archive.

Standard-library implementation: TF-IDF/cosine search, Lloyd K-Means and
Isolation Forest (random isolation trees). No NumPy, scikit-learn, PyTorch,
Transformers, paid API or model download is needed.
"""
from __future__ import annotations
import math, random, re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOP_WORDS = {"a","an","and","are","as","at","be","by","for","from","in","is","it","of","on","or","that","the","this","to","with","your","my","our","file","image","photo","document","memory"}

def _text(item: dict[str, Any]) -> str:
    parts = [item.get("file_name"), item.get("description"), item.get("category"), item.get("tags"), item.get("extracted_text"), item.get("ai_caption"), item.get("ai_labels"), item.get("location_name")]
    return " ".join(str(p).replace("_", " ").replace("-", " ") for p in parts if p)

def _tokens(text: str) -> list[str]:
    words = [w.lower() for w in _TOKEN_RE.findall(text or "")]
    words = [w for w in words if w not in _STOP_WORDS and len(w) > 1]
    return words + [f"{words[i]}_{words[i+1]}" for i in range(len(words)-1)]

def _age_days(item: dict[str, Any]) -> float:
    value = item.get("captured_at") or item.get("created_at")
    if not value: return 0.0
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError: return 0.0
    if not isinstance(value, datetime): return 0.0
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc)-value.astimezone(timezone.utc)).total_seconds()/86400)

def _vectorize(items: list[dict[str, Any]]):
    texts = [_text(x) or str(x.get("file_name") or "untitled") for x in items]
    docs = [_tokens(t) for t in texts]
    df: Counter[str] = Counter()
    for doc in docs: df.update(set(doc))
    n = max(1, len(docs)); vectors=[]
    for doc in docs:
        counts=Counter(doc); vec={}
        for token,count in counts.items(): vec[token]=(1+math.log(count))*(math.log((1+n)/(1+df[token]))+1)
        norm=math.sqrt(sum(v*v for v in vec.values()))
        vectors.append({k:v/norm for k,v in vec.items()} if norm else {})
    return vectors, sorted(df), texts

def _cosine(a: dict[str,float], b: dict[str,float]) -> float:
    if len(a)>len(b): a,b=b,a
    return sum(v*b.get(k,0.0) for k,v in a.items())

def _query_vector(query: str, vectors: list[dict[str,float]], vocabulary: list[str]):
    df: Counter[str]=Counter()
    for vec in vectors: df.update(vec.keys())
    n=max(1,len(vectors)); allowed=set(vocabulary); counts=Counter(_tokens(query)); vec={}
    for token,count in counts.items():
        if token in allowed: vec[token]=(1+math.log(count))*(math.log((1+n)/(1+df[token]))+1)
    norm=math.sqrt(sum(v*v for v in vec.values()))
    return {k:v/norm for k,v in vec.items()} if norm else {}

def search_library(items: list[dict[str,Any]], query: str, limit: int=30) -> list[dict[str,Any]]:
    query=(query or "").strip()
    if not items or not query: return []
    vectors,vocabulary,_=_vectorize(items); qvec=_query_vector(query,vectors,vocabulary)
    ranked=sorted(enumerate(_cosine(qvec,v) for v in vectors),key=lambda p:p[1],reverse=True)
    out=[]
    for i,score in ranked[:max(1,min(int(limit),100))]:
        if score<=0: continue
        row=dict(items[i]); row["ml_score"]=round(float(score),4); row["search_method"]="TF-IDF + cosine similarity (pure Python)"; out.append(row)
    return out

def recommend_related(items: list[dict[str,Any]], limit: int=8) -> list[dict[str,Any]]:
    if len(items)<2: return []
    vectors,_,_=_vectorize(items); candidates=[]
    for i,item in enumerate(items):
        best_i=-1; best=0.0
        for j,vec in enumerate(vectors):
            if i==j: continue
            score=_cosine(vectors[i],vec)
            if score>best: best_i,best=j,score
        if best_i>=0 and best>0.01: candidates.append({"source":dict(item),"match":dict(items[best_i]),"score":round(best,3)})
    candidates.sort(key=lambda x:x["score"],reverse=True); out=[]; used=set()
    for item in candidates:
        key=item["match"].get("id",id(item["match"]))
        if key in used: continue
        out.append(item); used.add(key)
        if len(out)>=max(1,min(int(limit),30)): break
    return out

def _dist2(a:dict[str,float],b:dict[str,float])->float:
    return sum((a.get(k,0.0)-b.get(k,0.0))**2 for k in set(a)|set(b))

def _mean(vectors:list[dict[str,float]])->dict[str,float]:
    sums:defaultdict[str,float]=defaultdict(float)
    for v in vectors:
        for k,x in v.items(): sums[k]+=x
    return {k:x/len(vectors) for k,x in sums.items()} if vectors else {}

def cluster_library(items: list[dict[str,Any]]) -> list[dict[str,Any]]:
    """Fit deterministic Lloyd K-Means on sparse TF-IDF vectors."""
    n=len(items)
    if n<4:return []
    vectors,vocabulary,_=_vectorize(items); unique={tuple(sorted(v.items())) for v in vectors}
    k=max(2,min(8,int(round(math.sqrt(n/2))),n-1,len(unique)))
    if k<2:return []
    rng=random.Random(42); centers=[dict(vectors[rng.randrange(n)])]
    while len(centers)<k:
        ds=[min(_dist2(v,c) for c in centers) for v in vectors]; total=sum(ds)
        if total<=1e-12:
            cand=next((v for v in vectors if all(_dist2(v,c)>1e-12 for c in centers)),vectors[len(centers)%n])
        else:
            target=rng.random()*total; accum=0.0; cand=vectors[-1]
            for v,d in zip(vectors,ds):
                accum+=d
                if accum>=target:cand=v;break
        centers.append(dict(cand))
    labels=[-1]*n
    for _ in range(60):
        newlabels=[min(range(k),key=lambda c:_dist2(v,centers[c])) for v in vectors]
        changed=newlabels!=labels; labels=newlabels
        centers=[_mean([vectors[i] for i,l in enumerate(labels) if l==c]) or dict(vectors[rng.randrange(n)]) for c in range(k)]
        if not changed:break
    groups=[]
    for label in sorted(set(labels)):
        indices=[i for i,l in enumerate(labels) if l==label]; center=centers[label]
        terms=[t for t,_ in sorted(center.items(),key=lambda p:p[1],reverse=True)[:4] if t in set(vocabulary)]
        groups.append({"id":label+1,"label":", ".join(terms[:3]) or f"Group {label+1}","terms":terms,"count":len(indices),"items":[dict(items[i]) for i in indices[:12]]})
    groups.sort(key=lambda g:g["count"],reverse=True);return groups

def _features(item:dict[str,Any])->list[float]:
    size=max(0.0,float(item.get("file_size") or 0)); width=max(0.0,float(item.get("width") or 0)); height=max(0.0,float(item.get("height") or 0)); duration=max(0.0,float(item.get("duration_seconds") or 0)); typ=item.get("media_type")
    return [math.log1p(size),math.log1p(width),math.log1p(height),math.log1p(duration),math.log1p(_age_days(item)),1.0 if typ=="photo" else 2.0 if typ=="video" else 3.0]

def _c_factor(n:int)->float:
    if n<=1:return 0.0
    # Harmonic-number approximation from the standard Isolation Forest normalization.
    return 2*(math.log(n-1)+0.5772156649)-2*(n-1)/n

def _tree(rows:list[list[float]],indices:list[int],depth:int,max_depth:int,rng:random.Random):
    if depth>=max_depth or len(indices)<=1:return ("leaf",len(indices))
    options=[]
    for f in range(len(rows[0])):
        vals=[rows[i][f] for i in indices]; lo,hi=min(vals),max(vals)
        if hi>lo:options.append((f,lo,hi))
    if not options:return ("leaf",len(indices))
    f,lo,hi=rng.choice(options); split=rng.uniform(lo,hi)
    left=[i for i in indices if rows[i][f]<split]; right=[i for i in indices if rows[i][f]>=split]
    if not left or not right:return ("leaf",len(indices))
    return ("node",f,split,_tree(rows,left,depth+1,max_depth,rng),_tree(rows,right,depth+1,max_depth,rng))

def _path(tree,row,depth=0):
    if tree[0]=="leaf":return depth+_c_factor(tree[1])
    _,f,split,left,right=tree
    return _path(left if row[f]<split else right,row,depth+1)

def detect_anomalies(items:list[dict[str,Any]],limit:int=12)->list[dict[str,Any]]:
    """Fit a compact Isolation Forest; anomaly flags never trigger file actions."""
    n=len(items)
    if n<8:return []
    raw=[_features(x) for x in items]; means=[sum(r[c] for r in raw)/n for c in range(len(raw[0]))]
    stds=[]
    for c,m in enumerate(means):
        stds.append(math.sqrt(sum((r[c]-m)**2 for r in raw)/n) or 1.0)
    rows=[[(v-means[c])/stds[c] for c,v in enumerate(r)] for r in raw]
    sample_size=min(128,n); max_depth=max(1,math.ceil(math.log2(sample_size))); tree_count=min(64,max(24,int(math.sqrt(n)*4)))
    rng=random.Random(42); all_indices=list(range(n)); trees=[]
    for _ in range(tree_count):
        sample=all_indices if n<=sample_size else rng.sample(all_indices,sample_size)
        trees.append(_tree(rows,sample,0,max_depth,rng))
    norm=_c_factor(sample_size) or 1.0; found=[]
    for i,row in enumerate(rows):
        avg=sum(_path(t,row) for t in trees)/len(trees); score=2**(-avg/norm)
        if score>0.60:
            result=dict(items[i]); result["anomaly_score"]=round(score,5); result["anomaly_reason"]=_anomaly_reason(items[i],items); found.append(result)
    found.sort(key=lambda x:x["anomaly_score"],reverse=True);return found[:max(1,min(int(limit),100))]

def _anomaly_reason(item:dict[str,Any],items:list[dict[str,Any]])->str:
    sizes=sorted(max(0,int(x.get("file_size") or 0)) for x in items); size=max(0,int(item.get("file_size") or 0)); median=float(sizes[len(sizes)//2]) if sizes else 0
    if median and (size>median*8 or size<median/8):return "Unusual file size compared with your archive"
    pixels=max(0,int(item.get("width") or 0))*max(0,int(item.get("height") or 0))
    if pixels>80_000_000:return "Unusual image dimensions"
    if item.get("media_type")=="video" and float(item.get("duration_seconds") or 0)>0:return "Unusual combination of video duration and file size"
    return "Unusual combination of archive metadata; review before taking action"

def build_intelligence(items:list[dict[str,Any]],query:str="")->dict[str,Any]:
    return {"search_method":"TF-IDF + cosine similarity (pure Python; no model download)","total":len(items),"text_ready":sum(1 for x in items if _text(x).strip()),"clusters":cluster_library(items),"anomalies":detect_anomalies(items),"recommendations":recommend_related(items),"query_results":search_library(items,query) if query else [],"ml_algorithms":[{"name":"TF-IDF + cosine similarity","purpose":"Data-dependent archive search and related-memory recommendations"},{"name":"K-Means (Lloyd's algorithm)","purpose":"Unsupervised grouping of sparse text vectors"},{"name":"Isolation Forest (random isolation trees)","purpose":"Outlier detection over file size, dimensions, age and duration"}]}
