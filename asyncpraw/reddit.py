"""Provide the Reddit class."""
import asyncio
import configparser
import os
import re
from itertools import islice
from logging import getLogger
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Iterable,
    Optional,
    Type,
    Union,
)
from warnings import warn

from asyncprawcore import (
    Authorizer,
    DeviceIDAuthorizer,
    ReadOnlyAuthorizer,
    Redirect,
    Requestor,
    ScriptAuthorizer,
    TrustedAuthenticator,
    UntrustedAuthenticator,
    session,
)
from asyncprawcore.exceptions import BadRequest

from . import models
from .config import Config
from .const import API_PATH, USER_AGENT_FORMAT, __version__
from .exceptions import (
    ClientException,
    MissingRequiredAttributeException,
    RedditAPIException,
)
from .objector import Objector
from .util.token_manager import BaseTokenManager

try:
    from update_checker import update_check

    UPDATE_CHECKER_MISSING = False
except ImportError:  # pragma: no cover
    UPDATE_CHECKER_MISSING = True

if TYPE_CHECKING:  # pragma: no cover
    from .. import asyncpraw

Comment = models.Comment
Redditor = models.Redditor
Submission = models.Submission
Subreddit = models.Subreddit

logger = getLogger("asyncpraw")


class Reddit:
    """The Reddit class provides convenient access to Reddit's API.

    Instances of this class are the gateway to interacting with Reddit's API through
    Async PRAW. The canonical way to obtain an instance of this class is via:

    .. code-block:: python

        import asyncpraw

        reddit = asyncpraw.Reddit(
            client_id="CLIENT_ID",
            client_secret="CLIENT_SECRET",
            password="PASSWORD",
            user_agent="USERAGENT",
            username="USERNAME",
        )

    """

    update_checked = False
    _ratelimit_regex = re.compile(r"([0-9]{1,2}) (seconds?|minutes?)")

    @property
    def _next_unique(self) -> int:
        value = self._unique_counter
        self._unique_counter += 1
        return value

    @property
    def read_only(self) -> bool:
        """Return True when using the ReadOnlyAuthorizer."""
        return self._core == self._read_only_core

    @read_only.setter
    def read_only(self, value: bool) -> None:
        """Set or unset the use of the ReadOnlyAuthorizer.

        :raises: :class:`ClientException` when attempting to unset ``read_only`` and
            only the ReadOnlyAuthorizer is available.

        """
        if value:
            self._core = self._read_only_core
        elif self._authorized_core is None:
            raise ClientException(
                "read_only cannot be unset as only the ReadOnlyAuthorizer is available."
            )
        else:
            self._core = self._authorized_core

    @property
    def validate_on_submit(self) -> bool:
        """Get validate_on_submit.

        .. deprecated:: 7.0

            If property :attr:`.validate_on_submit` is set to False, the behavior is
            deprecated by Reddit. This attribute will be removed around May-June 2020.

        """
        value = self._validate_on_submit
        if value is False:
            warn(
                "Reddit will check for validation on all posts around May-June 2020. It"
                " is recommended to check for validation by setting"
                " reddit.validate_on_submit to True.",
                category=DeprecationWarning,
                stacklevel=3,
            )
        return value

    @validate_on_submit.setter
    def validate_on_submit(self, val: bool):
        self._validate_on_submit = val

    async def __aenter__(self):
        """Handle the context manager open."""
        return self

    async def __aexit__(self, *_args):
        """Handle the context manager close."""
        await self.close()

    def __enter__(self):
        """Handle the context manager open.

        .. deprecated:: 7.1.1

            Using this class as a synchronous context manager is deprecated and will be
            removed in the next release. Use this class as an asynchronous context
            manager instead.

        """
        warn(
            "Using this class as a synchronous context manager is deprecated and will"
            " be removed in the next release. Use this class as an asynchronous context"
            " manager instead.",
            category=DeprecationWarning,
            stacklevel=3,
        )
        return self  # pragma: no cover

    def __exit__(self, *_args):
        """Handle the context manager close."""

    async def close(self):
        """Close the requestor."""
        await self.requestor.close()

    def __init__(
        self,
        site_name: str = None,
        config_interpolation: Optional[str] = None,
        requestor_class: Optional[Type[Requestor]] = None,
        requestor_kwargs: Dict[str, Any] = None,
        *,
        token_manager: Optional[BaseTokenManager] = None,
        **config_settings: Union[str, bool],
    ):  # noqa: D207, D301
        """Initialize a Reddit instance.

        :param site_name: The name of a section in your ``praw.ini`` file from which to
            load settings from. This parameter, in tandem with an appropriately
            configured ``praw.ini``, file is useful if you wish to easily save
            credentials for different applications, or communicate with other servers
            running Reddit. If ``site_name`` is ``None``, then the site name will be
            looked for in the environment variable praw_site. If it is not found there,
            the DEFAULT site will be used.
        :param requestor_class: A class that will be used to create a requestor. If not
            set, use ``asyncprawcore.Requestor`` (default: None).
        :param requestor_kwargs: Dictionary with additional keyword arguments used to
            initialize the requestor (default: None).
        :param token_manager: When provided, the passed instance, a subclass of
            :class:`.BaseTokenManager`, will manage tokens via two callback functions.
            This parameter must be provided in order to work with refresh tokens
            (default: None).

        Additional keyword arguments will be used to initialize the :class:`.Config`
        object. This can be used to specify configuration settings during instantiation
        of the :class:`.Reddit` instance. For more details, please see
        :ref:`configuration`.

        Required settings are:

        - client_id
        - client_secret (for installed applications set this value to ``None``)
        - user_agent

        The ``requestor_class`` and ``requestor_kwargs`` allow for customization of the
        requestor :class:`.Reddit` will use. This allows, e.g., easily adding behavior
        to the requestor or wrapping its |ClientSession|_ in a caching layer. Example
        usage:

        .. |ClientSession| replace:: ``ClientSession``

        .. _clientsession: https://docs.aiohttp.org/en/stable/client_advanced.html

        .. code-block:: python

            import json

            import aiohttp
            from asyncprawcore import Requestor

            from asyncpraw import Reddit


            class JSONDebugRequestor(Requestor):
                async def request(self, *args, **kwargs):
                    response = await super().request(*args, **kwargs)
                    print(json.dumps(await response.json(), indent=4))
                    return response


            my_session = aiohttp.ClientSession(trust_env=True)
            reddit = Reddit(
                ..., requestor_class=JSONDebugRequestor, requestor_kwargs={"session": my_session}
            )

        You can automatically close the requestor session by using this class as an
        context manager:

        .. code-block:: python

            async with Reddit(...) as reddit:
                print(await reddit.user.me())

        You can also call :meth:`.Reddit.close`:

        .. code-block:: python

            reddit = Reddit(...)
            # do stuff with reddit
            ...
            # then close the requestor when done
            await reddit.close()

        """
        self._core = self._authorized_core = self._read_only_core = None
        self._objector = None
        self._token_manager = token_manager
        self._unique_counter = 0
        self._validate_on_submit = False

        try:
            config_section = site_name or os.getenv("praw_site") or "DEFAULT"
            self.config = Config(
                config_section, config_interpolation, **config_settings
            )
        except configparser.NoSectionError as exc:
            help_message = (
                "You provided the name of a praw.ini configuration which does not"
                " exist.\n\nFor help with creating a Reddit instance,"
                " visit\nhttps://asyncpraw.readthedocs.io/en/latest/code_overview/reddit_instance.html\n\nFor"
                " help on configuring Async PRAW,"
                " visit\nhttps://asyncpraw.readthedocs.io/en/latest/getting_started/configuration.html"
            )
            if site_name is not None:
                exc.message += f"\n{help_message}"
            raise

        required_message = (
            "Required configuration setting {!r} missing. \nThis setting can be"
            " provided in a praw.ini file, as a keyword argument to the `Reddit` class"
            " constructor, or as an environment variable."
        )
        for attribute in ("client_id", "user_agent"):
            if getattr(self.config, attribute) in (self.config.CONFIG_NOT_SET, None):
                raise MissingRequiredAttributeException(
                    required_message.format(attribute)
                )
        if self.config.client_secret is self.config.CONFIG_NOT_SET:
            raise MissingRequiredAttributeException(
                f"{required_message.format('client_secret')}\nFor installed"
                " applications this value must be set to None via a keyword argument"
                " to the `Reddit` class constructor."
            )
        self._check_for_update()
        self._prepare_objector()
        self.requestor = self._prepare_asyncprawcore(requestor_class, requestor_kwargs)

        self.auth = models.Auth(self, None)
        """An instance of :class:`.Auth`.

        Provides the interface for interacting with installed and web applications.

        .. seealso::

            :ref:`auth_url`

        """

        self.front = models.Front(self)
        """An instance of :class:`.Front`.

        Provides the interface for interacting with front page listings. For example:

        .. code-block:: python

            async for submission in reddit.front.hot():
                print(submission)

        """

        self.inbox = models.Inbox(self, None)
        """An instance of :class:`.Inbox`.

        Provides the interface to a user's inbox which produces :class:`.Message`,
        :class:`.Comment`, and :class:`.Submission` instances. For example, to iterate
        through comments which mention the authorized user run:

        .. code-block:: python

            async for comment in reddit.inbox.mentions():
                print(comment)

        """

        self.live = models.LiveHelper(self, None)
        """An instance of :class:`.LiveHelper`.

        Provides the interface for working with :class:`.LiveThread` instances. At
        present only new LiveThreads can be created.

        .. code-block:: python

            await reddit.live.create("title", "description")

        """

        self.multireddit = models.MultiredditHelper(self, None)
        """An instance of :class:`.MultiredditHelper`.

        Provides the interface to working with :class:`.Multireddit` instances. For
        example, you can obtain a :class:`.Multireddit` instance via:

        .. code-block:: python

            multireddit = await reddit.multireddit("samuraisam", "programming")

        If you want to obtain a fetched :class:`.Multireddit` instance you can do:

        .. code-block:: python

            multireddit = await reddit.multireddit("samuraisam", "programming", fetch=True)

        """

        self.redditors = models.Redditors(self, None)
        """An instance of :class:`.Redditors`.

        Provides the interface for Redditor discovery. For example, to iterate over the
        newest Redditors, run:

        .. code-block:: python

            async for redditor in reddit.redditors.new(limit=None):
                print(redditor)

        """

        self.subreddit = models.SubredditHelper(self, None)
        """An instance of :class:`.SubredditHelper`.

        Provides the interface to working with :class:`.Subreddit` instances. For
        example, to create a Subreddit run:

        .. code-block:: python

            await reddit.subreddit.create("coolnewsubname")

        To obtain a lazy :class:`.Subreddit` instance run:

        .. code-block:: python

            await reddit.subreddit("redditdev")

        To obtain a fetched :class:`.Subreddit` instance run:

        .. code-block:: python

            await reddit.subreddit("redditdev", fetch=True)

        Multiple subreddits can be combined and filtered views of r/all can also be used
        just like a subreddit:

        .. code-block:: python

            await reddit.subreddit("redditdev+learnpython+botwatch")
            await reddit.subreddit("all-redditdev-learnpython")

        """

        self.subreddits = models.Subreddits(self, None)
        """An instance of :class:`.Subreddits`.

        Provides the interface for :class:`.Subreddit` discovery. For example, to
        iterate over the set of default subreddits run:

        .. code-block:: python

            async for subreddit in reddit.subreddits.default(limit=None):
                print(subreddit)

        """

        self.user = models.User(self)
        """An instance of :class:`.User`.

        Provides the interface to the currently authorized :class:`.Redditor`. For
        example, to get the name of the current user run:

        .. code-block:: python

            print(await reddit.user.me())

        """

    def _check_for_update(self):
        if UPDATE_CHECKER_MISSING:
            return
        if not Reddit.update_checked and self.config.check_for_updates:
            update_check(__package__, __version__)
            Reddit.update_checked = True

    def _prepare_common_authorizer(self, authenticator):
        if self._token_manager is not None:
            if self.config._do_not_use_refresh_token != self.config.CONFIG_NOT_SET:
                raise TypeError(
                    "legacy ``refresh_token`` setting cannot be provided when providing"
                    " ``token_manager``"
                )

            self._token_manager.reddit = self
            authorizer = Authorizer(
                authenticator,
                post_refresh_callback=self._token_manager.post_refresh_callback,
                pre_refresh_callback=self._token_manager.pre_refresh_callback,
            )
        elif self.config._do_not_use_refresh_token != self.config.CONFIG_NOT_SET:
            authorizer = Authorizer(
                authenticator, refresh_token=self.config._do_not_use_refresh_token
            )
        else:
            self._core = self._read_only_core
            return
        self._core = self._authorized_core = session(authorizer)

    def _prepare_objector(self):
        mappings = {
            self.config.kinds["comment"]: models.Comment,
            self.config.kinds["message"]: models.Message,
            self.config.kinds["redditor"]: models.Redditor,
            self.config.kinds["submission"]: models.Submission,
            self.config.kinds["subreddit"]: models.Subreddit,
            self.config.kinds["trophy"]: models.Trophy,
            "Button": models.Button,
            "Collection": models.Collection,
            "Image": models.Image,
            "LabeledMulti": models.Multireddit,
            "Listing": models.Listing,
            "LiveUpdate": models.LiveUpdate,
            "LiveUpdateEvent": models.LiveThread,
            "MenuLink": models.MenuLink,
            "ModeratedList": models.ModeratedList,
            "ModmailAction": models.ModmailAction,
            "ModmailConversation": models.ModmailConversation,
            "ModmailMessage": models.ModmailMessage,
            "Submenu": models.Submenu,
            "TrophyList": models.TrophyList,
            "UserList": models.RedditorList,
            "button": models.ButtonWidget,
            "calendar": models.Calendar,
            "community-list": models.CommunityList,
            "custom": models.CustomWidget,
            "id-card": models.IDCard,
            "image": models.ImageWidget,
            "menu": models.Menu,
            "modaction": models.ModAction,
            "moderator-list": models.ModeratorListing,
            "moderators": models.ModeratorsWidget,
            "more": models.MoreComments,
            "post-flair": models.PostFlairWidget,
            "rule": models.Rule,
            "stylesheet": models.Stylesheet,
            "subreddit-rules": models.RulesWidget,
            "textarea": models.TextArea,
            "widget": models.Widget,
        }
        self._objector = Objector(self, mappings)

    def _prepare_asyncprawcore(self, requestor_class=None, requestor_kwargs=None):
        requestor_class = requestor_class or Requestor
        requestor_kwargs = requestor_kwargs or {}

        requestor = requestor_class(
            USER_AGENT_FORMAT.format(self.config.user_agent),
            self.config.oauth_url,
            self.config.reddit_url,
            **requestor_kwargs,
        )

        if self.config.client_secret:
            self._prepare_trusted_asyncprawcore(requestor)
        else:
            self._prepare_untrusted_asyncprawcore(requestor)

        return requestor

    def _prepare_trusted_asyncprawcore(self, requestor):
        authenticator = TrustedAuthenticator(
            requestor,
            self.config.client_id,
            self.config.client_secret,
            self.config.redirect_uri,
        )
        read_only_authorizer = ReadOnlyAuthorizer(authenticator)
        self._read_only_core = session(read_only_authorizer)

        if self.config.username and self.config.password:
            script_authorizer = ScriptAuthorizer(
                authenticator, self.config.username, self.config.password
            )
            self._core = self._authorized_core = session(script_authorizer)
        else:
            self._prepare_common_authorizer(authenticator)

    def _prepare_untrusted_asyncprawcore(self, requestor):
        authenticator = UntrustedAuthenticator(
            requestor, self.config.client_id, self.config.redirect_uri
        )
        read_only_authorizer = DeviceIDAuthorizer(authenticator)
        self._read_only_core = session(read_only_authorizer)
        self._prepare_common_authorizer(authenticator)

    async def comment(
        self,  # pylint: disable=invalid-name
        id: Optional[str] = None,  # pylint: disable=redefined-builtin
        url: Optional[str] = None,
        lazy: bool = False,
    ):
        """Return an instance of :class:`~.Comment`.

        :param id: The ID of the comment.
        :param url: A permalink pointing to the comment.
        :param lazy: If True, object is loaded lazily (default: False).

        If you don't need the object fetched right away (e.g., to utilize a class
        method) then you can do:

        .. code-block:: python

            comment = await reddit.comment("comment_id", lazy=True)
            await comment.reply("reply")

        .. note::

            If call this with ``lazy=True`` and you need to obtain the comment's
            replies, you will need to call this without ``lazy=True`` or call
            :meth:`~.Comment.refresh` on the returned :class:`.Comment`.

        """
        comment = models.Comment(self, id=id, url=url)
        if not lazy:
            await comment._fetch()
        return comment

    def domain(self, domain: str):
        """Return an instance of :class:`.DomainListing`.

        :param domain: The domain to obtain submission listings for.

        """
        return models.DomainListing(self, domain)

    async def get(
        self,
        path: str,
        params: Optional[Union[str, Dict[str, Union[str, int]]]] = None,
    ):
        """Return parsed objects returned from a GET request to ``path``.

        :param path: The path to fetch.
        :param params: The query parameters to add to the request (default: None).

        """
        return await self._objectify_request(method="GET", params=params, path=path)

    def info(
        self,
        fullnames: Optional[Iterable[str]] = None,
        url: Optional[str] = None,
        subreddits: Optional[Iterable[Union["asyncpraw.models.Subreddit", str]]] = None,
    ) -> AsyncGenerator[
        Union[
            "asyncpraw.models.Subreddit",
            "asyncpraw.models.Comment",
            "asyncpraw.models.Submission",
        ],
        None,
    ]:
        """Fetch information about each item in ``fullnames``, ``url``, or ``subreddits``.

        :param fullnames: A list of fullnames for comments, submissions, and/or
            subreddits.
        :param url: A url (as a string) to retrieve lists of link submissions from.
        :param subreddits: A list of subreddit names or Subreddit objects to retrieve
            subreddits from.

        :returns: A generator that yields found items in their relative order.

        Items that cannot be matched will not be generated. Requests will be issued in
        batches for each 100 fullnames.

        .. note::

            For comments that are retrieved via this method, if you want to obtain its
            replies, you will need to call :meth:`~.Comment.refresh` on the yielded
            :class:`.Comment`.

        .. note::

            When using the URL option, it is important to be aware that URLs are treated
            literally by Reddit's API. As such, the URLs "youtube.com" and
            "https://www.youtube.com" will provide a different set of submissions.

        """
        none_count = (fullnames, url, subreddits).count(None)
        if none_count != 2:
            raise TypeError(
                "Either `fullnames`, `url`, or `subreddits` must be provided."
            )

        is_using_fullnames = fullnames is not None
        ids_or_names = fullnames if is_using_fullnames else subreddits

        if ids_or_names is not None:
            if isinstance(ids_or_names, str):
                raise TypeError(
                    "`fullnames` and `subreddits` must be a non-str iterable."
                )

            api_parameter_name = "id" if is_using_fullnames else "sr_name"

            async def generator(names):
                if is_using_fullnames:
                    iterable = iter(names)
                else:
                    iterable = iter([str(item) for item in names])
                while True:
                    chunk = list(islice(iterable, 100))
                    if not chunk:
                        break
                    params = {api_parameter_name: ",".join(chunk)}
                    for result in await self.get(API_PATH["info"], params=params):
                        yield result

            return generator(ids_or_names)

        async def generator(url):
            params = {"url": url}
            for result in await self.get(API_PATH["info"], params=params):
                yield result

        return generator(url)

    async def _objectify_request(
        self,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        files: Optional[Dict[str, IO]] = None,
        json=None,
        method: str = "",
        params: Optional[Union[str, Dict[str, str]]] = None,
        path: str = "",
    ) -> Any:
        """Run a request through the ``Objector``.

        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param files: Dictionary, filename to file (like) object mapping (default:
            None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.
        :param method: The HTTP method (e.g., GET, POST, PUT, DELETE).
        :param params: The query parameters to add to the request (default: None).
        :param path: The path to fetch.

        """
        return self._objector.objectify(
            await self.request(
                data=data,
                files=files,
                json=json,
                method=method,
                params=params,
                path=path,
            )
        )

    def _handle_rate_limit(
        self, exception: RedditAPIException
    ) -> Optional[Union[int, float]]:
        for item in exception.items:
            if item.error_type == "RATELIMIT":
                amount_search = self._ratelimit_regex.search(item.message)
                if not amount_search:
                    break
                seconds = int(amount_search.group(1))
                if "minute" in amount_search.group(2):
                    seconds *= 60
                if seconds <= int(self.config.ratelimit_seconds):
                    sleep_seconds = seconds + min(seconds / 10, 1)
                    return sleep_seconds
        return None

    async def delete(
        self,
        path: str,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        json=None,
    ) -> Any:
        """Return parsed objects returned from a DELETE request to ``path``.

        :param path: The path to fetch.
        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.

        """
        return await self._objectify_request(
            data=data, json=json, method="DELETE", path=path
        )

    async def patch(
        self,
        path: str,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        json=None,
    ) -> Any:
        """Return parsed objects returned from a PATCH request to ``path``.

        :param path: The path to fetch.
        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.

        """
        return await self._objectify_request(
            data=data, method="PATCH", path=path, json=json
        )

    async def post(
        self,
        path: str,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        files: Optional[Dict[str, IO]] = None,
        params: Optional[Union[str, Dict[str, str]]] = None,
        json=None,
    ) -> Any:
        """Return parsed objects returned from a POST request to ``path``.

        :param path: The path to fetch.
        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param files: Dictionary, filename to file (like) object mapping (default:
            None).
        :param params: The query parameters to add to the request (default: None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.

        """
        if json is None:
            data = data or {}
        try:
            return await self._objectify_request(
                data=data,
                files=files,
                json=json,
                method="POST",
                params=params,
                path=path,
            )
        except RedditAPIException as exception:
            seconds = self._handle_rate_limit(exception=exception)
            if seconds is not None:
                logger.debug(f"Rate limit hit, sleeping for {seconds:.2f} seconds")
                await asyncio.sleep(seconds)
                return await self._objectify_request(
                    data=data,
                    files=files,
                    method="POST",
                    params=params,
                    path=path,
                )
            raise

    async def put(
        self,
        path: str,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        json=None,
    ):
        """Return parsed objects returned from a PUT request to ``path``.

        :param path: The path to fetch.
        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.

        """
        return await self._objectify_request(
            data=data, json=json, method="PUT", path=path
        )

    async def random_subreddit(
        self, nsfw: bool = False
    ) -> "asyncpraw.models.Subreddit":
        """Return a random instance of :class:`~.Subreddit`.

        :param nsfw: Return a random NSFW (not safe for work) subreddit (default:
            False).

        """
        url = API_PATH["subreddit"].format(subreddit="randnsfw" if nsfw else "random")
        path = None
        try:
            await self.get(url, params={"unique": self._next_unique})
        except Redirect as redirect:
            path = redirect.path
        subreddit = models.Subreddit(self, path.split("/")[2])
        await subreddit._fetch()
        return subreddit

    async def redditor(
        self,
        name: Optional[str] = None,
        fullname: Optional[str] = None,
        fetch: bool = False,
    ) -> "asyncpraw.models.Redditor":
        """Return an instance of :class:`.Redditor`.

        :param name: The name of the redditor.
        :param fullname: The fullname of the redditor, starting with ``t2_``.

        Either ``name`` or ``fullname`` can be provided, but not both.

        """
        redditor = models.Redditor(self, name=name, fullname=fullname)
        if fetch:
            await redditor._fetch()
        return redditor

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Union[str, Dict[str, Union[str, int]]]] = None,
        data: Optional[Union[Dict[str, Union[str, Any]], bytes, IO, str]] = None,
        files: Optional[Dict[str, IO]] = None,
        json=None,
    ) -> Any:
        """Return the parsed JSON data returned from a request to URL.

        :param method: The HTTP method (e.g., GET, POST, PUT, DELETE).
        :param path: The path to fetch.
        :param params: The query parameters to add to the request (default: None).
        :param data: Dictionary, bytes, or file-like object to send in the body of the
            request (default: None).
        :param files: Dictionary, filename to file (like) object mapping (default:
            None).
        :param json: JSON-serializable object to send in the body of the request with a
            Content-Type header of application/json (default: None). If ``json`` is
            provided, ``data`` should not be.

        """
        if data and json:
            raise ClientException("At most one of `data` and `json` is supported.")
        try:
            return await self._core.request(
                method,
                path,
                data=data,
                files=files,
                params=params,
                timeout=self.config.timeout,
                json=json,
            )
        except BadRequest as exception:
            try:
                data = await exception.response.json(content_type=None)
            except ValueError:
                text = await exception.response.text()
                if text:
                    data = {"reason": text}
                else:
                    raise exception
            if set(data) == {"error", "message"}:
                raise
            explanation = data.get("explanation")
            if "fields" in data:
                assert len(data["fields"]) == 1
                field = data["fields"][0]
            else:
                field = None
            raise RedditAPIException(
                [data["reason"], explanation, field]
            ) from exception

    async def submission(  # pylint: disable=invalid-name,redefined-builtin
        self, id: Optional[str] = None, url: Optional[str] = None, lazy=False
    ) -> "asyncpraw.models.Submission":
        """Return an instance of :class:`~.Submission`.

        :param id: A Reddit base36 submission ID, e.g., ``2gmzqe``.
        :param url: A URL supported by
            :meth:`~asyncpraw.models.Submission.id_from_url`.`.
        :param lazy: If True, object is loaded lazily (default: False).

        Either ``id`` or ``url`` can be provided, but not both.

        If you don't need the object fetched right away (e.g., to utilize a class
        method) then you can do:

        .. code-block:: python

            submission = await reddit.submission("submission_id", lazy=True)
            await submission.mod.remove()

        """
        submission = models.Submission(self, id=id, url=url)
        if not lazy:
            await submission._fetch()
        return submission

    async def username_available(self, name: str) -> bool:
        """Check to see if the username is available.

        For example, to check if the username ``bboe`` is available, try:

        .. code-block:: python

            await reddit.username_available("bboe")

        """
        return await self._objectify_request(
            path=API_PATH["username_available"], params={"user": name}, method="GET"
        )
