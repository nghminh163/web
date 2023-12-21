import sentry_sdk
import pickle
from fastapi.responses import HTMLResponse
from html5lib.html5parser import parse
from html5lib import serialize
from loguru import logger

from server.utils.error import generate_error
from server.utils.logger_trace import trace
from server.utils.notify import send_message
from server.utils.cache import aio_redis_cache
from server.utils.utils import correct_url, safe_check_redis_connection
from server import MediumParser, base_template, config, medium_parser_exceptions, url_correlation, redis_storage, is_valid_medium_post_id_hexadecimal, postleter_template, medium_cache


@trace
@aio_redis_cache(10 * 60)
async def render_postleter(limit: int = 60, as_html: bool = False):
    random_post_id_list = [i[0] for i in medium_cache.random(limit)]

    outlenget_posts_list = []
    for post_id in random_post_id_list:
        try:
            post = MediumParser(post_id, timeout=config.TIMEOUT, host_address=config.HOST_ADDRESS)
            await post.query()
            post_metadata = await post.generate_metadata(as_dict=True)
            outlenget_posts_list.append(post_metadata)
        except Exception as ex:
            logger.error(f"Couldn't render post_id for postleter: {post_id}, ex: {ex}")
            # await send_message(f"Couldn't render post_id for postleter: {post_id}, ex: {ex}")

    postleter_template_rendered = await postleter_template.render_async(post_list=outlenget_posts_list)
    if as_html:
        return postleter_template_rendered
    return HTMLResponse(postleter_template_rendered)


@trace
async def render_medium_post_link(path: str, use_cache: bool = True):
    redis_available = await safe_check_redis_connection(redis_storage)

    try:
        if is_valid_medium_post_id_hexadecimal(path):
            medium_parser = MediumParser(path, timeout=config.TIMEOUT, host_address=config.HOST_ADDRESS)
        else:
            url = correct_url(path)
            medium_parser = await MediumParser.from_url(url, timeout=config.TIMEOUT, host_address=config.HOST_ADDRESS)
        medium_post_id = medium_parser.post_id
        if redis_available and use_cache:
            redis_result = await redis_storage.get(medium_post_id)
        else:
            redis_result = None
        if not redis_result:
            await medium_parser.query(use_cache=use_cache)
            rendered_medium_post = await medium_parser.render_as_html(minify=False, template_folder="server/templates")
        else:
            rendered_medium_post = pickle.loads(redis_result)
    except medium_parser_exceptions.InvalidURL as ex:
        logger.exception(ex)
        sentry_sdk.capture_exception(ex)
        return await generate_error(
            "Unable to identify the Medium article URL.",
            status_code=404,
        )
    except (medium_parser_exceptions.InvalidMediumPostURL, medium_parser_exceptions.MediumPostQueryError, medium_parser_exceptions.PageLoadingError) as ex:
        logger.exception(ex)
        sentry_sdk.capture_exception(ex)
        return await generate_error(
            "Unable to identify the link as a Medium.com article page. Please check the URL for any typing errors.",
            status_code=404,
        )
    except medium_parser_exceptions.InvalidMediumPostID as ex:
        logger.exception(ex)
        sentry_sdk.capture_exception(ex)
        return await generate_error("Unable to identify the Medium article ID.", status_code=500)
    except medium_parser_exceptions.NotValidMediumURL as ex:
        return await generate_error("You sure that this is a valid Medium.com URL?", status_code=404, quiet=True)
    except Exception as ex:
        logger.exception(ex)
        sentry_sdk.capture_exception(ex)
        return await generate_error(status_code=500)
    else:
        base_context = {
            "enable_ads_header": config.ENABLE_ADS_BANNER,
            "body_template": rendered_medium_post.data,
            "title": rendered_medium_post.title,
            "description": rendered_medium_post.description,
        }
        rendered_post = await base_template.render_async(base_context, HOST_ADDRESS=config.HOST_ADDRESS)
        parsed_rendered_post = parse(rendered_post)
        serialized_rendered_post = serialize(parsed_rendered_post, encoding='utf-8')

        if not redis_result:
            if not redis_available:
                await send_message("ERROR: Redis is not available. Please check your configuration.")
            else:
                await redis_storage.setex(medium_post_id, config.CACHE_LIFE_TIME, pickle.dumps(rendered_medium_post))
            await send_message(f"✅ Successfully rendered post: {url_correlation.get()}", True, "GOOD")

        return HTMLResponse(serialized_rendered_post)