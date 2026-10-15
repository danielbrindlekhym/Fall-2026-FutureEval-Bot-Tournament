"""Bounded operational release metadata; never copy forecast or reasoning fields."""
from copy import deepcopy
from datetime import datetime, timezone

FIELDS = ('post_id', 'question_id', 'published_at', 'open_time', 'scheduled_close_time', 'actual_close_time')
MAX_QUESTIONS = 3000
MAX_REVISIONS = 8


def when(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc).isoformat() if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def identity(value):
    return value if type(value) is int and value > 0 else None


def valid_metadata(meta, qid):
    return (isinstance(meta, dict) and set(meta) == set(FIELDS) and identity(meta['question_id']) == qid
        and (meta['post_id'] is None or identity(meta['post_id']) is not None)
        and all(value is None or when(value) == value for name,value in meta.items() if name.endswith('_time') or name == 'published_at'))


def release_inventory(posts, pages, checked_at, previous=None):
    at = when(checked_at)
    if at is None:
        raise ValueError('Release inventory requires an aware timestamp')
    if previous is not None and (not isinstance(previous, dict) or previous.get('version') != 1):
        raise ValueError('Unknown saved release inventory version; preserve for review')
    records = deepcopy((previous or {}).get('questions', {}))
    if not isinstance(records, dict) or len(records) > MAX_QUESTIONS:
        raise ValueError('Saved release inventory exceeds supported bounds')
    for key, record in records.items():
        if (not isinstance(record, dict) or identity(record.get('question_id')) is None or key != str(record.get('question_id'))
                or set(record) != {'question_id','first_seen_at','last_seen_at','first','latest','revisions','history_truncated'}
                or when(record['first_seen_at']) is None or when(record['first_seen_at']) != record['first_seen_at']
                or when(record['last_seen_at']) is None or when(record['last_seen_at']) != record['last_seen_at']
                or type(record['history_truncated']) is not bool
                or not isinstance(record['revisions'], list) or len(record['revisions']) > MAX_REVISIONS
                or any(not valid_metadata(item, record['question_id']) for item in [record['first'],record['latest']])
                or any(not isinstance(item, dict) or set(item) != {'at','metadata'}
                    or when(item['at']) is None or when(item['at']) != item['at']
                    or not valid_metadata(item['metadata'], record['question_id']) for item in record['revisions'])):
            raise ValueError('Saved release inventory schema changed; preserve for review')
    seen, unknown_groups, invalid_ids, announcements = set(), 0, 0, 0
    for post in posts:
        if not isinstance(post, dict):
            invalid_ids += 1
            continue
        q = post.get('question')
        if not isinstance(q, dict):
            if post.get('group_of_questions'):
                unknown_groups += 1
            else:
                announcements += 1
            continue
        qid = identity(q.get('id'))
        if qid is None or qid in seen:
            invalid_ids += 1
            continue
        seen.add(qid)
        meta = {'post_id': identity(post.get('id')), 'question_id': qid,
            'published_at': when(post.get('published_at')),
            'open_time': when(q.get('open_time') or post.get('open_time')),
            'scheduled_close_time': when(q.get('scheduled_close_time') or post.get('scheduled_close_time')),
            'actual_close_time': when(q.get('actual_close_time'))}
        key = str(qid)
        if key not in records:
            if len(records) >= MAX_QUESTIONS:
                raise ValueError('Release inventory question bound exceeded')
            records[key] = {'question_id':qid, 'first_seen_at':at, 'last_seen_at':at,
                'first':meta, 'latest':meta, 'revisions':[], 'history_truncated':False}
        else:
            record = records[key]
            if record['latest'] != meta:
                if len(record['revisions']) < MAX_REVISIONS:
                    record['revisions'].append({'at':at, 'metadata':meta})
                else:
                    record['history_truncated'] = True
                record['latest'] = meta
            record['last_seen_at'] = at
    safe_pages = []
    for page in pages or []:
        if (not isinstance(page, dict) or type(page.get('offset')) is not int
                or type(page.get('count')) is not int or type(page.get('has_next')) is not bool
                or page['offset'] < 0 or page['count'] < 0):
            raise ValueError('Invalid release pagination evidence')
        safe_pages.append({key:page[key] for key in ('offset','count','has_next')})
    page_complete = bool(safe_pages) and not safe_pages[-1]['has_next']
    page_complete = page_complete and safe_pages[0]['offset'] == 0 and all(
        b['offset'] == a['offset'] + a['count'] for a,b in zip(safe_pages,safe_pages[1:]))
    page_complete = page_complete and sum(page['count'] for page in safe_pages) == len(posts)
    page_complete = page_complete and all(page['has_next'] and page['count'] > 0 for page in safe_pages[:-1])
    return {'version':1, 'checked_at':at, 'questions':records, 'pages':safe_pages,
        'pagination_complete':page_complete, 'visible_question_ids':sorted(seen),
        'unrepresented_group_posts':unknown_groups, 'invalid_or_duplicate_question_records':invalid_ids,
        'nonquestion_posts':announcements, 'complete_official_released_inventory':False,
        'limitation':'Visible feed plus retained historical metadata; historical organizer visibility and full inventory remain unproven. No answer or reasoning fields.'}


def inventory_counts(inventory):
    return {'known_questions':len(inventory['questions']),
        'currently_visible_questions':len(inventory['visible_question_ids']),
        'pagination_complete':inventory['pagination_complete'],
        'unrepresented_group_posts':inventory['unrepresented_group_posts'],
        'invalid_or_duplicate_question_records':inventory['invalid_or_duplicate_question_records'],
        'unknown_publication_questions':sum(row['latest']['published_at'] is None for row in inventory['questions'].values()),
        'changed_metadata_questions':sum(bool(row['revisions']) for row in inventory['questions'].values()),
        'history_truncated_questions':sum(row['history_truncated'] for row in inventory['questions'].values()),
        'complete_official_released_inventory':False}
