import json
from datetime import datetime,timedelta,timezone
from . import storage
from .sources import search_djen


async def recover_queries(batch, metrics: dict, errors: list[str], now: datetime):
    queue=storage.get_state('djen_retry_queue').get('queries',[])
    keys={json.dumps(job['cursor'],sort_keys=True) for job in queue}
    for cursor in getattr(batch,'retry_cursors',[]):
        key=json.dumps(cursor,sort_keys=True)
        if key not in keys:
            queue.append({'cursor':cursor,'attempts':0,'next_at':(now+timedelta(minutes=30)).isoformat()})
            keys.add(key)
    due=next((job for job in queue if datetime.fromisoformat(job['next_at'])<=now),None)
    if due:
        cursor=due['cursor']
        result=await search_djen(datetime.fromisoformat(cursor['since']).date(),datetime.fromisoformat(cursor['until']).date(),cursor=cursor,stop_after_query=True)
        for row in result.rows:
            storage.upsert_document(row)
        metrics['djen_documents']+=len(result.rows)
        metrics['djen_pages']+=result.pages
        errors.extend(result.errors)
        if result.cursor is None and not result.errors:
            queue.remove(due)
        else:
            due['cursor']=result.cursor or cursor
            due['attempts']+=1
            due['next_at']=(now+timedelta(minutes=min(360,30*2**due['attempts']))).isoformat()
    storage.set_state('djen_retry_queue',{'queries':queue})
    metrics['djen_queries_pending']=len(queue)
    metrics['limited']=metrics['limited'] or bool(queue)
