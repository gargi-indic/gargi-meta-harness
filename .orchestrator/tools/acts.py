import json,sys
for a in json.load(sys.stdin):
  k=[x for x in a if x not in ('name','id','createTime','originator','description','artifacts')]
  print('---',a.get('originator'),k)
  if 'planGenerated' in a:
    for s in a['planGenerated']['plan'].get('steps',[]): print('*',s.get('title'),'::',s.get('description','')[:1200])
  elif 'agentMessaged' in a: print(a['agentMessaged'].get('agentMessage','')[:3000])
  elif 'progressUpdated' in a: print(a['progressUpdated'].get('title',''))
