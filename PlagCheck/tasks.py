from __future__ import absolute_import

import os

from celery import Celery

# set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'AuroraProject.settings')

from django.conf import settings
from django.db.utils import OperationalError

from PlagCheck.models import Reference, Result, Suspicion, SuspicionState, Store
from AuroraProject.settings import PLAGCHECK as plagcheck_settings
import sherlock

app = Celery('AuroraProject')

# Using a string here means the worker will not have to
# pickle the object when using Windows.
app.config_from_object('django.conf:settings')
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)


class PlagcheckError(Exception):
    def __init__(self, msg):
        self.msg = msg

@app.task(bind=True)
def check(self, **kwargs):
    """
    Do a check of a document against the hashed documents in the db.

    Call this function directly to run the check synchronously:

        tasks.check(args)

    or call this function asynchronously like:

        tasks.check.delay(args)

    to schedule a check at the celery worker process using a RabbitMQ message.

    The following are all required kwargs parameters that have to be passed.

    :param doc_id -- ID of the document within the Store

    :return: Future object of this task invocation when called asynchronously,
    or the result if called synchronously.
    """
    try:
        stored_doc = Store.objects.get(pk=kwargs['doc_id'])

        # delete existing references to older versions of this document
        Reference.remove_references(stored_doc.id)

        # generate a list of hashes
        hash_list = sherlock.signature_str(stored_doc.text)

        # remove duplicate hashes from the list
        hash_set = set(hash_list)

        hash_count = len(hash_set)

        # check if hashes are generated which means punctuations found
        suspicions = list()
        auto_filtered = False
        if hash_count > 0:
            # store (new) references
            Reference.store_references(stored_doc.id, hash_set)

            # compute individual similarity by computing the count
            # of matches for each other document. This returns a list
            # of possible matching documents.
            similar_elaborations = Reference.get_similar_elaborations(stored_doc.id)
            for (similar_doc_id, match_count) in similar_elaborations:
                similarity = round((100.0/hash_count) * match_count, 4)

                if similarity > 100:
                    raise PlagcheckError('computed similarity is greated than 100% ({0}). doc_id={5}, similar_doc_id={1}, hash_count={4}, match_count={2}'.format(similarity, similar_doc_id, match_count, hash_count, stored_doc.id))

                if similarity > plagcheck_settings['similarity_threshold'] \
                        and match_count > plagcheck_settings['minimal_match_count']:


                    # put them in a list so that filtered
                    # findings can be handled later
                    suspicions.append({
                        'similar_doc_id': similar_doc_id,
                        'similarity': similarity,
                        'match_count': match_count
                    })

        result = Result.objects.create(
            hash_count=hash_count,
            stored_doc_id=stored_doc.id,
            submission_time=stored_doc.submission_time.isoformat(),
        )

        # if there is a similar document that was assigned the state
        # FILTER then mark all suspecting elaborations as AUTO_FILTERED
        state = Suspicion.DEFAULT_STATE.value
        if auto_filtered:
            state = SuspicionState.AUTO_FILTERED.value

        for suspicion in suspicions:
            Suspicion.objects.create(
                stored_doc_id=stored_doc.id,
                similar_to_id=suspicion['similar_doc_id'],
                similarity=suspicion['similarity'],
                match_count=suspicion['match_count'],
                result=result,
                state=state
            )

        return result.celery_result()
    except OperationalError as e:
        print("Got an OperationalError, retrying")
        self.retry(exc=e, max_retries=2, countdown=5)
