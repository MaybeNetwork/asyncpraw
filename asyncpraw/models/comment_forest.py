"""Provide CommentForest for Submission comments."""
from heapq import heappop, heappush
from typing import TYPE_CHECKING, AsyncIterator, List, Optional, Union

from ..exceptions import DuplicateReplaceException
from .reddit.more import MoreComments

if TYPE_CHECKING:  # pragma: no cover
    from ... import asyncpraw


class CommentForest:
    """A forest of comments starts with multiple top-level comments.

    Each of these comments can be a tree of replies.

    """

    @staticmethod
    def _gather_more_comments(tree, parent_tree=None):
        """Return a list of MoreComments objects obtained from tree."""
        more_comments = []
        queue = [(None, x) for x in tree]
        while queue:
            parent, comment = queue.pop(0)
            if isinstance(comment, MoreComments):
                heappush(more_comments, comment)
                if parent:
                    comment._remove_from = parent.replies._comments
                else:
                    comment._remove_from = parent_tree or tree
            else:
                for item in comment.replies:
                    queue.append((comment, item))
        return more_comments

    def __getitem__(self, index: int):
        """Return the comment at position ``index`` in the list.

        This method is to be used like an array access, such as:

        .. code-block:: python

            comments = await submission.comments()
            first_comment = comments[0]

        Alternatively, the presence of this method enables one to iterate over all top
        level comments, like so:

        .. code-block:: python

            comments = await submission.comments()
            for comment in comments:
                print(comment.body)

        """
        return self._comments[index]

    async def __aiter__(self) -> AsyncIterator["asyncpraw.models.Comment"]:
        """Allow CommentForest to be used as an AsyncIterator.

        This method enables one to iterate over all top_level comments, like so:

        .. code-block:: python

            comments = await submission.comments()
            async for comment in comments:
                print(comment.body)

        """
        for comment in self._comments:
            yield comment

    def __init__(
        self,
        submission: "asyncpraw.models.Submission",
        comments: Optional[List["asyncpraw.models.Comment"]] = None,
    ):
        """Initialize a CommentForest instance.

        :param submission: An instance of :class:`~.Subreddit` that is the parent of the
            comments.
        :param comments: Initialize the Forest with a list of comments (default: None).

        """
        self._comments = comments
        self._submission = submission

    def __len__(self) -> int:
        """Return the number of top-level comments in the forest."""
        if self._comments:
            return len(self._comments)
        else:
            return 0

    def _insert_comment(self, comment):
        if comment.name in self._submission._comments_by_id:
            raise DuplicateReplaceException
        comment.submission = self._submission
        if isinstance(comment, MoreComments) or comment.is_root:
            self._comments.append(comment)
        else:
            assert comment.parent_id in self._submission._comments_by_id, (
                "Async PRAW Error occurred. Please file a bug report and include the"
                " code that caused the error."
            )
            parent = self._submission._comments_by_id[comment.parent_id]
            parent.replies._comments.append(comment)

    def _update(self, comments):
        self._comments = comments
        for comment in comments:
            comment.submission = self._submission

    async def list(
        self,
    ) -> List[Union["asyncpraw.models.Comment", "asyncpraw.models.MoreComments"]]:
        """Return a flattened list of all Comments.

        This list may contain :class:`.MoreComments` instances if :meth:`.replace_more`
        was not called first.

        """
        comments = []
        queue = list(self)
        while queue:
            comment = queue.pop(0)
            comments.append(comment)
            if not isinstance(comment, MoreComments):
                queue.extend([reply async for reply in comment.replies])
        return comments

    async def replace_more(
        self, limit: int = 32, threshold: int = 0
    ) -> List["asyncpraw.models.MoreComments"]:
        """Update the comment forest by resolving instances of MoreComments.

        :param limit: The maximum number of :class:`.MoreComments` instances to replace.
            Each replacement requires 1 API request. Set to ``None`` to have no limit,
            or to ``0`` to remove all :class:`.MoreComments` instances without
            additional requests (default: 32).
        :param threshold: The minimum number of children comments a
            :class:`.MoreComments` instance must have in order to be replaced.
            :class:`.MoreComments` instances that represent "continue this thread" links
            unfortunately appear to have 0 children. (default: 0).

        :returns: A list of :class:`.MoreComments` instances that were not replaced.

        :raises: ``asyncprawcore.TooManyRequests`` when used concurrently.

        For example, to replace up to 32 :class:`.MoreComments` instances of a
        submission try:

        .. code-block:: python

            submission = await reddit.submission("3hahrw", lazy=True)
            comments = await submission.comments()
            await comments.replace_more()

        Alternatively, to replace :class:`.MoreComments` instances within the replies of
        a single comment try:

        .. code-block:: python

            comment = await reddit.comment("d8r4im1")
            await comment.replies.replace_more()

        .. note::

            This method can take a long time as each replacement will discover at most
            100 new :class:`.Comment` instances. As a result, consider looping and
            handling exceptions until the method returns successfully. For example:

            .. code-block:: python

                while True:
                    try:
                        comments = await submission.comments()
                        await comments.replace_more()
                        break
                    except PossibleExceptions:
                        print("Handling replace_more exception")
                        await asyncio.sleep(1)

        .. warning::

            If this method is called, and the comments are refreshed, calling this
            method again will result in a :class:`.DuplicateReplaceException`.

        """
        remaining = limit
        more_comments = self._gather_more_comments(self._comments)
        skipped = []

        # Fetch largest more_comments until reaching the limit or the threshold
        while more_comments:
            item = heappop(more_comments)
            if remaining is not None and remaining <= 0 or item.count < threshold:
                skipped.append(item)
                item._remove_from.remove(item)
                continue

            new_comments = await item.comments(update=False)
            if remaining is not None:
                remaining -= 1

            # Add new MoreComment objects to the heap of more_comments
            for more in self._gather_more_comments(new_comments, self._comments):
                more.submission = self._submission
                heappush(more_comments, more)
            # Insert all items into the tree
            for comment in new_comments:
                self._insert_comment(comment)

            # Remove from forest
            item._remove_from.remove(item)

        return more_comments + skipped
